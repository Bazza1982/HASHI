from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict, deque

import pytest

from orchestrator.her_v2 import runtime_invocation as runtime_invocation_module
from orchestrator.her_v2.audit import DurableAuditLog
from orchestrator.her_v2.commentary import RecordingCommentaryPort
from orchestrator.her_v2.config import HERv2Config
from orchestrator.her_v2.interfaces import (
    ProviderFailureCode,
    RecordingDelivery,
    StageInvocationError,
)
from orchestrator.her_v2.ledger import LedgerStore
from orchestrator.her_v2.models import (
    Effort,
    Stage,
    StageResponse,
    TerminalState,
    ToolEvidenceReceipt,
    ToolReceiptStatus,
    TriageClassification,
)
from orchestrator.her_v2.presentation import (
    RenderedRequiredMessage,
    RequiredUserMessage,
)
from orchestrator.her_v2.progress import ProviderActivityTracker
from orchestrator.her_v2.runtime import HERv2Runtime
from orchestrator.multimodal_contract import canonical_request_content


def _config(**overrides):
    profiles = {
        name: {
            "engine": "fake-api",
            "model": f"model-{name}",
            "reasoning": f"reasoning-{name}",
        }
        for name in ("lightweight", "triage", "premium", "reviewer", "orchestrator")
    }
    raw = {
        "profiles": profiles,
        "user_idle_timeout_s": 10,
        "shadow_mode": False,
    }
    raw.update(overrides)
    return HERv2Config.from_mapping(raw)


def _scripted_verification_response(profile, request, value):
    data = dict(value)
    prefix = request.invocation_id or (
        f"{request.turn_id}:{request.stage.value}:{request.attempt}"
    )
    before_ref = f"test-tool:{prefix}:snapshot-before"
    after_ref = f"test-tool:{prefix}:snapshot-after"
    receipts = [
        ToolEvidenceReceipt(
            before_ref,
            Stage.VERIFICATION,
            prefix,
            request.attempt,
            "snapshot-before",
            "workspace_inspect",
            ToolReceiptStatus.SUCCESS,
            True,
            True,
            "before-output",
            {"operation": "snapshot", "snapshot_sha256": "stable"},
        )
    ]
    checks = []
    for index, raw_check in enumerate(data.get("checks") or [], start=1):
        check = dict(raw_check)
        result = str(check.get("result") or check.get("status") or "").upper()
        method = str(check.get("method") or "workspace_diff")
        if result in {"VERIFIED", "PARTIALLY_VERIFIED", "FAILED"}:
            evidence_ref = f"test-tool:{prefix}:check-{index}"
            check.setdefault("evidence_refs", [evidence_ref])
            workspace_test = method == "workspace_test"
            failed = result == "FAILED" and workspace_test
            receipts.append(
                ToolEvidenceReceipt(
                    evidence_ref,
                    Stage.VERIFICATION,
                    prefix,
                    request.attempt,
                    f"check-{index}",
                    "verification_run" if workspace_test else "workspace_inspect",
                    ToolReceiptStatus.FAILED if failed else ToolReceiptStatus.SUCCESS,
                    True,
                    True,
                    f"check-{index}-output",
                    (
                        {
                            "operation": "run",
                            "recipe": "pytest_core",
                            "exit_code": 1 if failed else 0,
                            "execution_scope": "workspace",
                            "workspace_copied": False,
                            "process_isolated": False,
                            "process_authority": "inherited",
                            "identity_policy": "inherited",
                            "filesystem_policy": "inherited",
                            "environment_policy": "inherited",
                            "network_policy": "inherited",
                            "home_policy": "inherited",
                            "workspace_access": {
                                "read": True,
                                "write": True,
                                "execute": True,
                            },
                            "timeout_s": 1800.0,
                            "timeout_policy": {
                                "execution_floor_s": 300.0,
                                "effective_timeout_s": 1800.0,
                            },
                        }
                        if workspace_test
                        else {"operation": "diff", "exit_code": 0}
                    ),
                )
            )
        checks.append(check)
    data["checks"] = checks
    receipts.append(
        ToolEvidenceReceipt(
            after_ref,
            Stage.VERIFICATION,
            prefix,
            request.attempt,
            "snapshot-after",
            "workspace_inspect",
            ToolReceiptStatus.SUCCESS,
            True,
            True,
            "after-output",
            {"operation": "snapshot", "snapshot_sha256": "stable"},
        )
    )
    data.setdefault(
        "evidence_refs",
        [
            receipt.evidence_ref
            for receipt in receipts[1:-1]
            if receipt.completed
        ],
    )
    return StageResponse(
        text="",
        data=data,
        reasoning_trace=f"trace:{request.stage.value}",
        provider=profile.engine,
        model=profile.model,
        provider_attempt=request.attempt,
        evidence_refs=tuple(item.evidence_ref for item in receipts),
        tool_receipts=tuple(receipts),
    )


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
        if request.stage is Stage.VERIFICATION and isinstance(value, dict):
            return _scripted_verification_response(profile, request, value)
        if request.stage is Stage.REVIEW and isinstance(value, dict):
            value = dict(value)
            prefix = request.invocation_id or (
                f"{request.turn_id}:{request.stage.value}:{request.attempt}"
            )
            before_ref = f"test-tool:{prefix}:snapshot-before"
            inspection_ref = f"test-tool:{prefix}:inspection"
            after_ref = f"test-tool:{prefix}:snapshot-after"
            value.setdefault("evidence_refs", [inspection_ref])
            receipts = (
                ToolEvidenceReceipt(
                    before_ref,
                    Stage.REVIEW,
                    prefix,
                    request.attempt,
                    "snapshot-before",
                    "workspace_inspect",
                    ToolReceiptStatus.SUCCESS,
                    True,
                    True,
                    "before-output",
                    {"operation": "snapshot", "snapshot_sha256": "stable"},
                ),
                ToolEvidenceReceipt(
                    inspection_ref,
                    Stage.REVIEW,
                    prefix,
                    request.attempt,
                    "inspection",
                    "workspace_inspect",
                    ToolReceiptStatus.SUCCESS,
                    True,
                    True,
                    "inspection-output",
                    {"operation": "diff", "exit_code": 0},
                ),
                ToolEvidenceReceipt(
                    after_ref,
                    Stage.REVIEW,
                    prefix,
                    request.attempt,
                    "snapshot-after",
                    "workspace_inspect",
                    ToolReceiptStatus.SUCCESS,
                    True,
                    True,
                    "after-output",
                    {"operation": "snapshot", "snapshot_sha256": "stable"},
                ),
            )
            return StageResponse(
                text="",
                data=value,
                reasoning_trace=f"trace:{request.stage.value}",
                provider=profile.engine,
                model=profile.model,
                provider_attempt=request.attempt,
                evidence_refs=tuple(item.evidence_ref for item in receipts),
                tool_receipts=receipts,
            )
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
    retry_policy=None,
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
        retry_policy=retry_policy,
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


def _multimodal_request_content(
    tmp_path,
    *,
    attachment_ids=("attachment-1", "attachment-2"),
):
    return canonical_request_content(
        [
            {"type": "text", "item_index": 1, "text": "Compare both."},
            {
                "type": "media",
                "item_index": 2,
                "attachment_id": attachment_ids[0],
                "modality": "image",
                "kind": "photo",
                "mime_type": "image/png",
                "filename": "one.png",
                "caption": "",
                "local_ref": str(tmp_path / "one.png"),
                "size_bytes": 10,
                "sha256": "1" * 64,
                "transport": {"message_id": 1},
            },
            {
                "type": "media",
                "item_index": 3,
                "attachment_id": attachment_ids[1],
                "modality": "image",
                "kind": "photo",
                "mime_type": "image/png",
                "filename": "two.png",
                "caption": "",
                "local_ref": str(tmp_path / "two.png"),
                "size_bytes": 11,
                "sha256": "2" * 64,
                "transport": {"message_id": 2},
            },
        ]
    )


def _native_media_routes():
    return (
        {
            "attachment_id": "attachment-1",
            "item_index": 2,
            "modality": "image",
            "route": "native",
            "reason": "native_capability_available",
            "transport": "data_url",
        },
        {
            "attachment_id": "attachment-2",
            "item_index": 3,
            "modality": "image",
            "route": "native",
            "reason": "native_capability_available",
            "transport": "data_url",
        },
    )


@pytest.mark.asyncio
async def test_direct_response_and_triage_receive_same_ordered_images(tmp_path):
    routes = _native_media_routes()
    provider = ScriptedProvider(
        {
            Stage.IMMEDIATE_RESPONSE: [
                StageResponse(data={"message": "They differ."}, media_routing=routes)
            ],
            Stage.TRIAGE: [
                StageResponse(
                    data={"classification": "DIRECT_RESPONSE"},
                    media_routing=routes,
                )
            ],
        }
    )
    content = _multimodal_request_content(tmp_path)

    result = await _runtime(tmp_path, provider).run_turn(
        "Compare both.",
        "request-native-direct",
        effort="low",
        request_content=content,
    )

    assert result.classification is TriageClassification.DIRECT_RESPONSE
    assert result.final_was_immediate is True
    foreground = [request for _profile, request in provider.requests]
    assert {request.stage for request in foreground} == {
        Stage.IMMEDIATE_RESPONSE,
        Stage.TRIAGE,
    }
    assert all(request.request_content == content for request in foreground)
    assert all(
        [item["attachment_id"] for item in request.attachment_manifest]
        == ["attachment-1", "attachment-2"]
        for request in foreground
    )


@pytest.mark.asyncio
async def test_unfulfillable_multimodal_direct_response_uses_work_path(tmp_path):
    scripts = {
        Stage.IMMEDIATE_RESPONSE: [
            StageInvocationError(
                "native media unavailable",
                retryable=False,
                code=ProviderFailureCode.PROVIDER_MODALITY_UNSUPPORTED,
            )
        ],
        Stage.TRIAGE: [
            StageResponse(data={"classification": "DIRECT_RESPONSE"})
        ],
        Stage.EXECUTION: [
            StageResponse(
                data={"disposition": "COMPLETED", "summary": "Inspected both."},
                media_routing=tuple(
                    {**route, "route": "local_fallback"}
                    for route in _native_media_routes()
                ),
            )
        ],
        Stage.FINALISATION: [{"report": "Inspected both."}],
    }
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Compare both.",
        "request-media-fallback",
        effort="low",
        request_content=_multimodal_request_content(tmp_path),
    )

    assert result.terminal_state is TerminalState.COMPLETED
    assert result.classification is TriageClassification.SIMPLE_TASK
    assert result.final_was_immediate is False
    assert Stage.EXECUTION in {
        request.stage for _profile, request in provider.requests
    }


@pytest.mark.asyncio
async def test_subagent_receives_only_authorized_attachment_subset(tmp_path):
    scripts = _initial("HIGH_VOLUME_TASK")
    scripts.update(
        {
            Stage.PLANNING: [
                {
                    "plan": ["inspect the first image", "synthesise"],
                    "sub_agents": [
                        {
                            "id": "first-only",
                            "task": "Inspect the first image only",
                            "profile": "lightweight",
                            "tools": [],
                            "attachment_ids": ["attachment-1"],
                        }
                    ],
                }
            ],
            Stage.EXECUTION: [
                {
                    "disposition": "COMPLETED",
                    "summary": "First image inspected.",
                },
                {
                    "disposition": "COMPLETED",
                    "summary": "Primary synthesis completed.",
                },
            ],
            Stage.REVIEW: [{"outcome": "PASS", "summary": "Acceptable."}],
            Stage.FINALISATION: [{"report": "Completed."}],
        }
    )
    provider = ScriptedProvider(scripts)
    content = _multimodal_request_content(tmp_path)
    aggregate_goal = (
        f"Compare {tmp_path / 'one.png'} with {tmp_path / 'two.png'} and report."
    )
    content_parts = [dict(part) for part in content["parts"]]
    content_parts[0]["text"] = aggregate_goal
    content = canonical_request_content(content_parts)

    result = await _runtime(tmp_path, provider).run_turn(
        aggregate_goal,
        "request-bounded-media",
        effort=Effort.MAX,
        request_content=content,
    )

    sub_request = next(
        request
        for _profile, request in provider.requests
        if request.role == "sub_agent:first-only"
    )
    assert result.terminal_state is TerminalState.COMPLETED
    assert [
        item["attachment_id"] for item in sub_request.attachment_manifest
    ] == ["attachment-1"]
    assert [
        item["attachment_id"]
        for item in sub_request.context["authorized_attachment_manifest"]
    ] == ["attachment-1"]
    assert str(tmp_path / "two.png") not in sub_request.goal
    assert all(
        part.get("attachment_id") != "attachment-2"
        for part in sub_request.request_content["parts"]
    )
    assert all(
        str(tmp_path / "two.png") not in str(part.get("text") or "")
        for part in sub_request.request_content["parts"]
        if part.get("type") == "text"
    )


@pytest.mark.asyncio
async def test_compare_all_subagent_receives_every_image_in_original_order(tmp_path):
    scripts = _initial("HIGH_VOLUME_TASK")
    scripts.update(
        {
            Stage.PLANNING: [
                {
                    "plan": ["compare all images", "synthesise"],
                    "sub_agents": [
                        {
                            "id": "compare-all",
                            "task": "Compare all images and report their differences",
                            "profile": "lightweight",
                            "tools": [],
                        }
                    ],
                }
            ],
            Stage.EXECUTION: [
                {
                    "disposition": "COMPLETED",
                    "summary": "All images compared.",
                },
                {
                    "disposition": "COMPLETED",
                    "summary": "Primary synthesis completed.",
                },
            ],
            Stage.REVIEW: [{"outcome": "PASS", "summary": "Acceptable."}],
            Stage.FINALISATION: [{"report": "Completed."}],
        }
    )
    provider = ScriptedProvider(scripts)
    content = _multimodal_request_content(
        tmp_path,
        attachment_ids=("z-first", "a-second"),
    )

    result = await _runtime(tmp_path, provider).run_turn(
        "Compare every image.",
        "request-compare-all-media",
        effort=Effort.MAX,
        request_content=content,
    )

    sub_request = next(
        request
        for _profile, request in provider.requests
        if request.role == "sub_agent:compare-all"
    )
    assert result.terminal_state is TerminalState.COMPLETED
    assert [
        item["attachment_id"] for item in sub_request.attachment_manifest
    ] == ["z-first", "a-second"]
    assert sub_request.context["authorized_attachment_ids"] == [
        "z-first",
        "a-second",
    ]


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
async def test_combined_finalisation_delivers_persona_message_without_second_renderer(
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
            Stage.FINALISATION: [
                {
                    "execution_result": {
                        "disposition": "COMPLETED",
                        "summary": "Completed with receipt job-42.",
                    },
                    "final_message": raw_report,
                }
            ],
        }
    )
    renderer = RecordingRequiredPersonaRenderer()

    result = await _runtime(
        tmp_path,
        ScriptedProvider(scripts),
        required_persona=renderer,
    ).run_turn("Complete it", "request-render-final", effort="low")

    assert result.terminal_state is TerminalState.COMPLETED
    assert result.text == raw_report
    assert renderer.messages == []
    final = next(item for item in result.delivery_records if item.kind == "final")
    assert final.text == raw_report
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
    assert delivery["payload"]["provenance"] == "her_v2_combined_finalisation"


@pytest.mark.asyncio
async def test_removed_final_persona_renderer_cannot_change_finalisation_result(
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
    assert renderer.messages == []
    rows = [
        json.loads(line)
        for line in (tmp_path / "her-v2" / "audit.jsonl").read_text().splitlines()
    ]
    assert not any(
        row["event"].startswith("required_persona_render") for row in rows
    )


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
async def test_execution_receives_the_same_complete_request_context_as_planning(
    tmp_path,
):
    complete_context = (
        "RECENT TURN MESSAGES\n"
        "Memory+ recent facts\n"
        "Cross-session receipt evidence\n"
        "CURRENT USER REQUEST"
    )
    scripts = _initial("COMPLEX_TASK")
    scripts.update(
        {
            Stage.PLANNING: [
                {"plan": ["inspect", "change", "verify"]}
            ],
            Stage.EXECUTION: [
                {
                    "disposition": "COMPLETED",
                    "summary": "Completed from the supplied context.",
                }
            ],
            Stage.FINALISATION: [
                {
                    "execution_result": {
                        "disposition": "COMPLETED",
                        "summary": "Completed from the supplied context.",
                    },
                    "final_message": "Completed from the supplied context.",
                }
            ],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        complete_context, "request-complete-execution-context", effort="medium"
    )

    planning_request = next(
        request
        for _profile, request in provider.requests
        if request.stage is Stage.PLANNING
    )
    execution_request = next(
        request
        for _profile, request in provider.requests
        if request.stage is Stage.EXECUTION
    )
    finalisation_request = next(
        request
        for _profile, request in provider.requests
        if request.stage is Stage.FINALISATION
    )
    assert planning_request.goal == complete_context
    assert execution_request.goal == complete_context
    assert finalisation_request.goal == complete_context
    assert execution_request.context["active_plan"]["plan"] == [
        "inspect",
        "change",
        "verify",
    ]
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
    assert set(execution.context) == {"active_plan", "sub_agent_results"}
    assert execution.allow_tools is True
    assert execution.allow_side_effects is True
    assert result.terminal_state is TerminalState.COMPLETED


@pytest.mark.asyncio
async def test_review_imposed_replanning_does_not_reconsult_habits(tmp_path):
    scripts = _initial("COMPLEX_TASK")
    scripts.update(
        {
            Stage.PLANNING: [{"plan": ["old approach"]}],
            Stage.EXECUTION: [
                {
                    "disposition": "COMPLETED",
                    "summary": "Candidate used the old API.",
                    "evidence_refs": ["evidence:constraint"],
                },
                {
                    "disposition": "COMPLETED",
                    "summary": "Used the supported API.",
                    "evidence_refs": ["test:green"],
                },
            ],
            Stage.REVIEW: [
                {
                    "outcome": "FAIL",
                    "summary": "The old API is unavailable.",
                    "findings": ["Use the supported API."],
                },
                {
                    "outcome": "PASS",
                    "summary": "The supported API remediation is closed.",
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
        "Implement safely", "request-replan", effort=Effort.XHIGH
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
async def test_review_failure_cannot_change_valid_execution_disposition(tmp_path):
    scripts = _initial("COMPLEX_TASK")
    scripts.update(
        {
            Stage.PLANNING: [{"plan": ["attempt the constrained approach"]}],
            Stage.EXECUTION: [
                {
                    "disposition": "COMPLETED",
                    "summary": "The constrained candidate was produced.",
                }
            ],
            Stage.REVIEW: [
                {
                    "outcome": "FAIL",
                    "summary": "A required route is unavailable.",
                    "findings": ["The required verification route is unavailable."],
                }
            ],
            Stage.FINALISATION: [
                {"report": "Work completed with an unresolved verification limitation."}
            ],
        }
    )
    config = _config(replan_limits={"xhigh": 0})

    result = await _runtime(
        tmp_path, ScriptedProvider(scripts), config=config
    ).run_turn("Complete the constrained task", "request-no-replan", effort="xhigh")

    assert result.terminal_state is TerminalState.COMPLETED
    assert result.replan_count == 0
    assert "verification route" in result.limitations[0]


@pytest.mark.asyncio
async def test_xhigh_review_fail_performs_remediation_and_one_closure_review(tmp_path):
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
                },
                {"outcome": "PASS", "summary": "The missing check is now present."},
            ],
            Stage.REPLANNING: [{"plan": ["add check"]}],
            Stage.FINALISATION: [{"report": "Remediated and reported."}],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Build and review", "request-xhigh", effort=Effort.XHIGH
    )

    assert result.review_count == 2
    assert result.replan_count == 1
    reviews = [
        call for _profile, call in provider.requests if call.stage is Stage.REVIEW
    ]
    assert len(reviews) == 2
    assert all(review.allow_tools is True for review in reviews)
    assert all(review.allow_side_effects is False for review in reviews)
    assert [review.context["review_kind"] for review in reviews] == [
        "independent",
        "closure",
    ]


@pytest.mark.asyncio
async def test_max_assurance_remediation_and_verification_are_bounded_at_three(tmp_path):
    scripts = _initial("HIGH_VOLUME_TASK")
    scripts.update(
        {
            Stage.PLANNING: [{"plan": ["orchestrate"]}],
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": f"Candidate {index}."}
                for index in range(4)
            ],
            Stage.REVIEW: [{"outcome": "PASS", "summary": "Review passed."}],
            Stage.VERIFICATION: [
                {
                    "outcome": "FAILED",
                    "summary": f"Verification {index} failed.",
                    "checks": [
                        {
                            "claim": "Core tests pass",
                            "verifiability": "VERIFIABLE",
                            "result": "FAILED",
                            "method": "workspace_test",
                            "observed": f"Core tests failed on attempt {index}.",
                        }
                    ],
                }
                for index in range(1, 4)
            ],
            Stage.REPLANNING: [
                {"plan": [f"remediation {index}"]} for index in range(1, 3)
            ],
            Stage.FINALISATION: [{"report": "Assurance limit reached honestly."}],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Process the large batch", "request-max", effort=Effort.MAX
    )

    assert result.review_count == 1
    assert result.verification_count == 3
    assert result.assurance_status == "FAILED"
    assert result.replan_count == 2
    assert sum(call.stage is Stage.REVIEW for _profile, call in provider.requests) == 1
    assert (
        sum(
            call.stage is Stage.VERIFICATION
            for _profile, call in provider.requests
        )
        == 3
    )
    assert result.terminal_state is TerminalState.COMPLETED


@pytest.mark.asyncio
async def test_max_runs_review_then_comprehensive_assured_verification(tmp_path):
    def finalisation(request):
        assurance = request.context["assurance"]
        assert assurance["outcome"] == "VERIFIED"
        assert assurance["attempts"] == 1
        assert assurance["checks"][0]["result"] == "VERIFIED"
        return {"report": "Completed and independently verified."}

    scripts = _initial("COMPLEX_TASK")
    scripts.update(
        {
            Stage.PLANNING: [
                {
                    "plan": ["implement", "verify"],
                    "success_criteria": ["Core tests pass"],
                }
            ],
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": "Implementation completed."}
            ],
            Stage.REVIEW: [{"outcome": "PASS", "summary": "Review passed."}],
            Stage.VERIFICATION: [
                {
                    "outcome": "VERIFIED",
                    "summary": "Every required claim is verified.",
                    "checks": [
                        {
                            "claim": "Core tests pass",
                            "verifiability": "VERIFIABLE",
                            "result": "VERIFIED",
                            "method": "workspace_test",
                            "observed": "The workspace core recipe exited 0.",
                        }
                    ],
                }
            ],
            Stage.FINALISATION: [finalisation],
        }
    )
    provider = ScriptedProvider(scripts, delays={Stage.EXECUTION: 0.01})

    result = await _runtime(tmp_path, provider).run_turn(
        "Implement with assurance", "request-assured-pass", effort="assured"
    )

    assert result.terminal_state is TerminalState.COMPLETED
    assert result.review_count == 1
    assert result.verification_count == 1
    assert result.assurance_status == "VERIFIED"
    stages = [
        request.stage
        for _profile, request in provider.requests
        if request.stage
        in {
            Stage.PLANNING,
            Stage.EXECUTION,
            Stage.REVIEW,
            Stage.VERIFICATION,
            Stage.FINALISATION,
        }
    ]
    assert stages == [
        Stage.PLANNING,
        Stage.EXECUTION,
        Stage.REVIEW,
        Stage.VERIFICATION,
        Stage.FINALISATION,
    ]
    verification_request = next(
        request
        for _profile, request in provider.requests
        if request.stage is Stage.VERIFICATION
    )
    assert verification_request.allow_tools is True
    assert verification_request.allow_side_effects is True
    assert "verification_run" in verification_request.context["delegated_tools"]
    assert verification_request.context["verifier_authority"] == (
        "advisory_authoritative_workspace_verification"
    )
    assert verification_request.context["execution_elapsed_s"] >= 0.005
    assert verification_request.context["verification_run_policy"] == {
        "workspace": "authoritative_current_workspace",
        "workspace_copied": False,
        "process_authority": "inherited",
        "environment": "inherited",
        "network": "inherited",
        "timeout_basis": "cumulative_execution_elapsed",
    }


@pytest.mark.asyncio
async def test_max_failed_verification_remediates_then_checks_latest_execution(tmp_path):
    scripts = _initial("COMPLEX_TASK")
    scripts.update(
        {
            Stage.PLANNING: [{"plan": ["implement"]}],
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": "Initial implementation."},
                {"disposition": "COMPLETED", "summary": "Remediated implementation."},
            ],
            Stage.REVIEW: [{"outcome": "PASS", "summary": "Review passed."}],
            Stage.VERIFICATION: [
                {
                    "outcome": "FAILED",
                    "summary": "The workspace recipe failed.",
                    "checks": [
                        {
                            "claim": "Core tests pass",
                            "verifiability": "VERIFIABLE",
                            "result": "FAILED",
                            "method": "workspace_test",
                            "observed": "The workspace recipe exited 1.",
                        }
                    ],
                },
                {
                    "outcome": "VERIFIED",
                    "summary": "The remediated state passed.",
                    "checks": [
                        {
                            "claim": "Core tests pass",
                            "verifiability": "VERIFIABLE",
                            "result": "VERIFIED",
                            "method": "workspace_test",
                            "observed": "The workspace recipe exited 0.",
                        }
                    ],
                },
            ],
            Stage.REPLANNING: [{"plan": ["repair failing core test"]}],
            Stage.FINALISATION: [{"report": "Remediated and verified."}],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Implement and prove it", "request-assured-remediation", effort=Effort.MAX
    )

    assert result.terminal_state is TerminalState.COMPLETED
    assert result.review_count == 1
    assert result.verification_count == 2
    assert result.replan_count == 1
    assert result.assurance_status == "VERIFIED"
    verification_requests = [
        request
        for _profile, request in provider.requests
        if request.stage is Stage.VERIFICATION
    ]
    assert [item.context["verification_attempt"] for item in verification_requests] == [
        1,
        2,
    ]
    assert (
        verification_requests[1].context["execution"]["summary"]
        == "Remediated implementation."
    )
    assert (
        sum(
            request.stage is Stage.REVIEW
            for _profile, request in provider.requests
        )
        == 1
    )


@pytest.mark.asyncio
async def test_verification_technical_failure_is_unavailable_not_execution_failure(
    tmp_path,
):
    scripts = _initial("COMPLEX_TASK")
    scripts.update(
        {
            Stage.PLANNING: [{"plan": ["complete"]}],
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": "Execution completed."}
            ],
            Stage.REVIEW: [{"outcome": "PASS", "summary": "Review passed."}],
            Stage.VERIFICATION: [
                StageInvocationError("verifier offline", retryable=False)
            ],
            Stage.FINALISATION: [
                {"report": "Execution completed; Verification was unavailable."}
            ],
        }
    )

    result = await _runtime(tmp_path, ScriptedProvider(scripts)).run_turn(
        "Complete with assurance", "request-verification-unavailable", effort="max"
    )

    assert result.terminal_state is TerminalState.COMPLETED
    assert result.assurance_status == "UNAVAILABLE"
    assert result.verification_count == 1
    assert any("No current tool-backed Verification" in item for item in result.limitations)


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
                    "disposition": "FAILED",
                    "summary": "Source B could not be inspected because it moved.",
                    "limitations": ["Source B was unavailable."],
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
    assert "could not be inspected" in prohibited["summary"]
    assert result.terminal_state is TerminalState.COMPLETED
    assert result.evidence_refs == ("sub:a", "primary:synthesis")


@pytest.mark.asyncio
async def test_high_volume_subagents_have_no_fixed_count_ceiling(tmp_path):
    scripts = _initial("HIGH_VOLUME_TASK")
    scripts.update(
        {
            Stage.PLANNING: [
                {
                    "plan": ["delegate all independent work", "synthesise"],
                    "sub_agents": [
                        {"id": f"sub-{index}", "task": f"task {index}"}
                        for index in range(3)
                    ],
                }
            ],
            Stage.EXECUTION: [
                {"disposition": "COMPLETED", "summary": "Work completed."}
                for _index in range(4)
            ],
            Stage.REVIEW: [{"outcome": "PASS", "summary": "Verified."}],
            Stage.FINALISATION: [{"report": "All delegated work completed."}],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Large task", "request-subagents-unbounded", effort=Effort.MAX
    )

    assert result.terminal_state is TerminalState.COMPLETED
    assert sum(
        request.role.startswith("sub_agent:")
        for _profile, request in provider.requests
    ) == 3


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
async def test_finalisation_exhausts_one_recovery_without_repeating_execution(
    tmp_path,
):
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
                    StageInvocationError("report provider unavailable"),
                    StageInvocationError("report provider still unavailable"),
                ],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Perform once", "request-report", effort=Effort.LOW
    )

    assert result.terminal_state is TerminalState.ERROR
    assert result.evidence_refs == ("receipt:123",)
    assert sum(call.stage is Stage.EXECUTION for _profile, call in provider.requests) == 1
    finalisation_requests = [
        call
        for _profile, call in provider.requests
        if call.stage is Stage.FINALISATION
    ]
    assert len(finalisation_requests) == 2
    assert [call.attempt for call in finalisation_requests] == [1, 2]
    assert len({call.retry_invariant_hash for call in finalisation_requests}) == 1
    assert len(
        {
            call.context["execution_evidence_hash"]
            for call in finalisation_requests
        }
    ) == 1
    assert len(
        {call.context["finalisation_input_hash"] for call in finalisation_requests}
    ) == 1
    assert len(
        {call.context["execution_invocation_id"] for call in finalisation_requests}
    ) == 1
    assert "report provider still unavailable" in result.error
    assert "marked ERROR" in result.text
    assert "PROVIDER_UNKNOWN" in result.text


@pytest.mark.asyncio
async def test_failed_execution_keeps_truthful_terminal_state(tmp_path):
    disposition = "FAILED"
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
    assert result.terminal_state is TerminalState.FAILED


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
                    "disposition": "COMPLETED",
                    "summary": "The original route produced an unverified candidate.",
                    "evidence_refs": ["receipt:gone"],
                    "commentary": "The original route produced a candidate.",
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
                    "outcome": "FAIL",
                    "summary": "The original route is no longer supported.",
                    "findings": ["Use the supported replacement route."],
                    "commentary": "Independent review required a replacement route.",
                },
                {
                    "outcome": "PASS",
                    "summary": "The replacement route closed the finding.",
                },
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
        (Stage.EXECUTION, "The original route produced a candidate."),
        (Stage.REVIEW, "Independent review required a replacement route."),
        (Stage.REPLANNING, "A supported replacement route is now planned."),
        (
            Stage.EXECUTION,
            "The replacement route completed successfully.",
        ),
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
async def test_commentary_port_failure_does_not_block_execution_or_completion(
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
                    "disposition": "COMPLETED",
                    "summary": "The alternate route worked.",
                    "commentary": "The alternate route worked.",
                },
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
    assert result.replan_count == 0
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
        config=_config(user_idle_timeout_s=0.03),
    ).run_turn("Complete quickly", "request-slow-commentary", effort="low")

    assert result.terminal_state is TerminalState.COMPLETED
    assert [item.kind for item in result.delivery_records].count("final") == 1
    assert commentary.records == []


@pytest.mark.asyncio
async def test_stage_failure_does_not_synthesise_commentary_from_lifecycle_events(
    tmp_path,
):
    scripts = _initial("COMPLEX_TASK")
    scripts[Stage.PLANNING] = [
        StageInvocationError("planner provider unavailable", retryable=False)
    ]
    commentary = RecordingCommentaryPort()

    result = await _runtime(
        tmp_path,
        ScriptedProvider(scripts),
        commentary=commentary,
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
        config=_config(user_idle_timeout_s=0.05),
    ).run_turn("Long but active", "request-progress", effort="low")
    assert result.terminal_state is TerminalState.COMPLETED


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
    provider = ScriptedProvider(scripts)
    result = await _runtime(tmp_path, provider).run_turn(
        "Complete robustly", "request-review-error", effort=Effort.XHIGH
    )
    assert result.terminal_state is TerminalState.COMPLETED
    assert any("Review unavailable" in item for item in result.limitations)
    finalisation_request = next(
        request
        for _profile, request in provider.requests
        if request.stage is Stage.FINALISATION
    )
    assert finalisation_request.context["review"]["outcome"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_finalisation_retries_same_evidence_without_repeating_execution(
    tmp_path,
):
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
            Stage.FINALISATION: [
                StageInvocationError("report transport failed"),
                {"report": "Completed after recovery."},
            ],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Perform once", "request-report-retries", effort="low"
    )

    assert result.terminal_state is TerminalState.COMPLETED
    assert result.text == "Completed after recovery."
    assert result.evidence_refs == ("receipt:timeout-case",)
    assert sum(
        request.stage is Stage.EXECUTION for _profile, request in provider.requests
    ) == 1
    finalisation_requests = [
        request
        for _profile, request in provider.requests
        if request.stage is Stage.FINALISATION
    ]
    assert len(finalisation_requests) == 2
    assert [request.attempt for request in finalisation_requests] == [1, 2]
    assert len(
        {
            request.context["execution_evidence_hash"]
            for request in finalisation_requests
        }
    ) == 1
    assert len(
        {
            request.context["finalisation_input_hash"]
            for request in finalisation_requests
        }
    ) == 1
    rows = [
        json.loads(line)
        for line in (tmp_path / "her-v2" / "audit.jsonl").read_text().splitlines()
    ]
    retry = next(
        row
        for row in rows
        if row["event"] == "stage_retry_scheduled"
        and row["stage"] == Stage.FINALISATION.value
    )
    assert retry["payload"]["fresh_connection"] is True
    assert retry["payload"]["same_provider"] is True
    assert retry["payload"]["same_model"] is True
    assert retry["payload"]["same_goal"] is True
    assert retry["payload"]["same_classification"] is True
    assert retry["payload"]["same_permissions"] is True
    assert retry["payload"]["same_workzone"] is True


@pytest.mark.asyncio
async def test_provider_operations_have_no_elapsed_attempt_deadline(
    tmp_path, monkeypatch
):
    class RecoveryOnlyPolicy:
        max_provider_retries = 1

    observed_wait_timeouts = []
    real_wait = asyncio.wait

    class ControlledClock:
        now = 0.0

        def __call__(self):
            return self.now

    clock = ControlledClock()
    monkeypatch.setattr(
        runtime_invocation_module,
        "ProviderActivityTracker",
        lambda: ProviderActivityTracker(clock=clock),
    )

    async def tracked_wait(awaitables, **kwargs):
        observed_wait_timeouts.append(kwargs.get("timeout"))
        return await real_wait(awaitables, **kwargs)

    monkeypatch.setattr(asyncio, "wait", tracked_wait)
    crossed_boundaries = []

    async def execution_across_former_boundaries(request):
        for boundary in (60, 180, 190, 300, 600, 601):
            clock.now = float(boundary)
            crossed_boundaries.append(boundary)
            request.provider_activity_callback(
                {
                    "kind": "progress",
                    "content": f"work continued after {boundary} seconds",
                }
            )
            await asyncio.sleep(0)
        return {
            "disposition": "COMPLETED",
            "summary": "Completed after every former elapsed boundary.",
        }

    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [execution_across_former_boundaries],
            Stage.FINALISATION: [{"report": "Completed without a clock."}],
        }
    )

    result = await _runtime(
        tmp_path,
        ScriptedProvider(scripts),
        retry_policy=RecoveryOnlyPolicy(),
    ).run_turn(
        "Run without an elapsed ceiling",
        "request-no-attempt-deadline",
        effort="low",
    )

    assert result.terminal_state is TerminalState.COMPLETED, result.error
    assert crossed_boundaries == [60, 180, 190, 300, 600, 601]
    assert clock.now == 601
    assert observed_wait_timeouts
    assert all(timeout is None for timeout in observed_wait_timeouts)
    audit_text = (tmp_path / "her-v2" / "audit.jsonl").read_text()
    assert "attempt_timeout_s" not in audit_text
    assert "retry_tier" not in audit_text


@pytest.mark.real_wall_clock
@pytest.mark.skipif(
    os.environ.get("HASHI_RUN_REAL_WALL_CLOCK_CANARY") != "1",
    reason="set HASHI_RUN_REAL_WALL_CLOCK_CANARY=1 to run the 601-second canary",
)
@pytest.mark.asyncio
async def test_real_wall_clock_execution_crosses_largest_former_deadline(tmp_path):
    observed_elapsed = []

    async def execution_after_601_seconds(_request):
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.sleep(601)
        observed_elapsed.append(loop.time() - started)
        return {
            "disposition": "COMPLETED",
            "summary": "Completed beyond the former 600-second boundary.",
        }

    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [execution_after_601_seconds],
            Stage.FINALISATION: [
                {"report": "Completed beyond the former 600-second boundary."}
            ],
        }
    )

    result = await _runtime(tmp_path, ScriptedProvider(scripts)).run_turn(
        "Run beyond every former provider deadline",
        "request-real-wall-clock-canary",
        effort="low",
    )

    assert result.terminal_state is TerminalState.COMPLETED, result.error
    assert observed_elapsed and observed_elapsed[0] >= 600


@pytest.mark.asyncio
async def test_retry_after_is_not_rejected_by_a_fabricated_recovery_window(tmp_path):
    scripts = {
        Stage.IMMEDIATE_RESPONSE: [{"message": "Recovered."}],
        Stage.TRIAGE: [
            StageInvocationError(
                "temporarily rate limited",
                code=ProviderFailureCode.PROVIDER_RATE_LIMITED,
                retry_after_s=601,
            ),
            {"classification": "DIRECT_RESPONSE", "goal": "Reply"},
        ],
    }
    runtime = _runtime(tmp_path, ScriptedProvider(scripts))
    scheduled_delays = []

    async def record_without_waiting(*_args, **kwargs):
        scheduled_delays.append(kwargs["retry_delay"])

    runtime._wait_for_stage_retry = record_without_waiting

    result = await runtime.run_turn(
        "Reply after the provider permits recovery",
        "request-unbounded-retry-after",
        effort="low",
    )

    assert result.terminal_state is TerminalState.COMPLETED
    assert scheduled_delays == [601]


@pytest.mark.asyncio
async def test_nonretryable_auth_failure_keeps_typed_code_and_single_attempt(tmp_path):
    scripts = {
        Stage.IMMEDIATE_RESPONSE: [
            StageInvocationError(
                "credential rejected",
                retryable=False,
                code=ProviderFailureCode.PROVIDER_AUTHENTICATION_FAILED,
                human_description="The provider rejected the configured credentials.",
                http_status=401,
            )
        ],
        Stage.TRIAGE: [
            {"classification": "DIRECT_RESPONSE", "goal": "Reply directly"}
        ],
    }
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Reply directly", "request-auth-nonretry", effort="low"
    )

    assert result.terminal_state is TerminalState.ERROR
    assert ProviderFailureCode.PROVIDER_AUTHENTICATION_FAILED.value in result.error
    assert ProviderFailureCode.PROVIDER_AUTHENTICATION_FAILED.value in result.text
    assert sum(
        request.stage is Stage.IMMEDIATE_RESPONSE
        for _profile, request in provider.requests
    ) == 1
    rows = [
        json.loads(line)
        for line in (tmp_path / "her-v2" / "audit.jsonl").read_text().splitlines()
    ]
    failure = next(
        row
        for row in rows
        if row["event"] == "stage_attempt_failed"
        and row["stage"] == Stage.IMMEDIATE_RESPONSE.value
    )
    assert failure["payload"]["http_status"] == 401
    assert failure["payload"]["will_retry"] is False
    assert failure["payload"]["retry_reason"] == "failure_non_retryable"


@pytest.mark.asyncio
async def test_rate_limit_retry_honours_retry_after_and_preserves_route(tmp_path):
    scripts = {
        Stage.IMMEDIATE_RESPONSE: [{"message": "Recovered."}],
        Stage.TRIAGE: [
            StageInvocationError(
                "rate limited",
                code=ProviderFailureCode.PROVIDER_RATE_LIMITED,
                human_description="The provider rate-limited the request.",
                http_status=429,
                provider_request_id="provider-request-429",
                retry_after_s=0.001,
            ),
            {"classification": "DIRECT_RESPONSE", "goal": "Reply"},
        ],
    }
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Reply", "request-rate-limit", effort="low"
    )

    assert result.terminal_state is TerminalState.COMPLETED
    rows = [
        json.loads(line)
        for line in (tmp_path / "her-v2" / "audit.jsonl").read_text().splitlines()
    ]
    failure = next(
        row
        for row in rows
        if row["event"] == "stage_attempt_failed"
        and row["stage"] == Stage.TRIAGE.value
    )
    retry = next(
        row
        for row in rows
        if row["event"] == "stage_retry_scheduled"
        and row["stage"] == Stage.TRIAGE.value
    )
    assert failure["payload"]["provider_request_id"] == "provider-request-429"
    assert failure["payload"]["retry_after_s"] == pytest.approx(0.001)
    assert retry["payload"]["retry_delay_s"] == pytest.approx(0.001)
    assert retry["payload"]["same_provider"] is True
    assert retry["payload"]["same_model"] is True


@pytest.mark.asyncio
async def test_execution_retries_once_before_any_tool_starts(tmp_path):
    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [
                StageInvocationError(
                    "connection reset",
                    code=ProviderFailureCode.PROVIDER_CONNECTION_FAILED,
                    human_description="The provider connection was interrupted.",
                ),
                {"disposition": "COMPLETED", "summary": "Recovered safely."},
            ],
            Stage.FINALISATION: [{"report": "Recovered safely."}],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Inspect safely", "request-execution-pre-tool-retry", effort="low"
    )

    assert result.terminal_state is TerminalState.COMPLETED
    execution_requests = [
        request
        for _profile, request in provider.requests
        if request.stage is Stage.EXECUTION
        and not request.role.startswith("sub_agent:")
    ]
    assert [request.attempt for request in execution_requests] == [1, 2]


@pytest.mark.asyncio
async def test_execution_retries_after_only_proven_read_only_tools(tmp_path):
    async def read_then_disconnect(request):
        request.provider_activity_callback(
            {
                "kind": "tool_start",
                "content": "read",
                "tool_name": "file_read",
                "tool_read_only": True,
            }
        )
        request.provider_activity_callback(
            {
                "kind": "file_read",
                "content": "reading file",
                "tool_name": "file_read",
                "tool_read_only": True,
            }
        )
        request.provider_activity_callback(
            {
                "kind": "tool_end",
                "content": "read complete",
                "tool_name": "file_read",
                "tool_read_only": True,
            }
        )
        raise StageInvocationError(
            "connection reset after read",
            code=ProviderFailureCode.PROVIDER_CONNECTION_FAILED,
        )

    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [
                read_then_disconnect,
                {"disposition": "COMPLETED", "summary": "Recovered after read."},
            ],
            Stage.FINALISATION: [{"report": "Recovered after read."}],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Read and inspect", "request-execution-read-retry", effort="low"
    )

    assert result.terminal_state is TerminalState.COMPLETED
    assert sum(
        request.stage is Stage.EXECUTION
        for _profile, request in provider.requests
    ) == 2


@pytest.mark.asyncio
async def test_execution_never_replays_after_side_effect_tool_starts(tmp_path):
    async def write_then_disconnect(request):
        request.provider_activity_callback(
            {
                "kind": "tool_start",
                "content": "write",
                "tool_name": "file_write",
                "tool_read_only": False,
            }
        )
        request.provider_activity_callback(
            {
                "kind": "tool_end",
                "content": "write completed",
                "tool_name": "file_write",
                "tool_read_only": False,
                "tool_details": {
                    "foreground_cleanup": {
                        "status": "normal_completion",
                        "process_reaped": True,
                        "group_alive": False,
                        "errors": [],
                    }
                },
            }
        )
        raise StageInvocationError(
            "connection reset after write began",
            code=ProviderFailureCode.PROVIDER_CONNECTION_FAILED,
        )

    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [
                write_then_disconnect,
                {"disposition": "COMPLETED", "summary": "Must not run."},
            ],
            Stage.FINALISATION: [{"report": "Execution could not be replayed."}],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Write once", "request-execution-write-no-retry", effort="low"
    )

    assert result.terminal_state is TerminalState.ERROR
    assert ProviderFailureCode.PROVIDER_CONNECTION_FAILED.value in result.error
    assert ProviderFailureCode.PROVIDER_CONNECTION_FAILED.value in result.text
    assert ProviderFailureCode.SIDE_EFFECT_REPLAY_BLOCKED.value in result.text
    assert result.primary_failure["code"] == (
        ProviderFailureCode.PROVIDER_CONNECTION_FAILED.value
    )
    assert result.recovery_decision["code"] == (
        ProviderFailureCode.SIDE_EFFECT_REPLAY_BLOCKED.value
    )
    assert result.recovery_decision["automatic_replay_attempted"] is False
    assert result.foreground_cleanup["status"] == "normal_completion"
    assert "Foreground cleanup:" in result.text
    assert "Process reaped: yes" in result.text
    assert sum(
        request.stage is Stage.EXECUTION
        for _profile, request in provider.requests
    ) == 1
    rows = [
        json.loads(line)
        for line in (tmp_path / "her-v2" / "audit.jsonl").read_text().splitlines()
    ]
    failure = next(
        row
        for row in rows
        if row["event"] == "stage_attempt_failed"
        and row["stage"] == Stage.EXECUTION.value
    )
    assert failure["payload"]["error_code"] == (
        ProviderFailureCode.PROVIDER_CONNECTION_FAILED.value
    )
    assert failure["payload"]["recovery_decision"]["code"] == (
        ProviderFailureCode.SIDE_EFFECT_REPLAY_BLOCKED.value
    )


@pytest.mark.asyncio
async def test_cleanup_failure_is_disclosed_without_hiding_primary_failure(tmp_path):
    async def shell_cleanup_failed_then_disconnect(request):
        request.provider_activity_callback(
            {
                "kind": "shell_exec",
                "content": "bash started",
                "tool_name": "bash",
                "tool_read_only": False,
            }
        )
        request.provider_activity_callback(
            {
                "kind": "tool_end",
                "content": "bash cleanup failed",
                "tool_name": "bash",
                "tool_read_only": False,
                "tool_details": {
                    "foreground_cleanup": {
                        "status": "cleanup_failed",
                        "process_reaped": False,
                        "group_alive": True,
                        "errors": ["injected cleanup failure"],
                    }
                },
            }
        )
        raise StageInvocationError(
            "provider connection reset",
            code=ProviderFailureCode.PROVIDER_CONNECTION_FAILED,
            human_description="The provider connection was interrupted.",
        )

    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [shell_cleanup_failed_then_disconnect],
            Stage.FINALISATION: [{"report": "Execution failed."}],
        }
    )

    result = await _runtime(tmp_path, ScriptedProvider(scripts)).run_turn(
        "Run once", "request-cleanup-failure-truth", effort="low"
    )

    assert result.terminal_state is TerminalState.ERROR
    assert result.primary_failure["code"] == (
        ProviderFailureCode.PROVIDER_CONNECTION_FAILED.value
    )
    assert result.recovery_decision["code"] == (
        ProviderFailureCode.SIDE_EFFECT_REPLAY_BLOCKED.value
    )
    assert result.foreground_cleanup["status"] == "cleanup_failed"
    assert "Status: `cleanup_failed`" in result.text
    assert "Process reaped: no" in result.text
    assert "injected cleanup failure" in result.text


@pytest.mark.asyncio
async def test_execution_does_not_retry_with_incomplete_read_only_tool(tmp_path):
    async def second_read_stalls_then_disconnects(request):
        for kind in ("tool_start", "tool_end", "tool_start"):
            request.provider_activity_callback(
                {
                    "kind": kind,
                    "content": kind,
                    "tool_name": "file_read",
                    "tool_read_only": True,
                }
            )
        raise StageInvocationError(
            "connection reset during second read",
            code=ProviderFailureCode.PROVIDER_CONNECTION_FAILED,
        )

    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [
                second_read_stalls_then_disconnects,
                {"disposition": "COMPLETED", "summary": "Must not run."},
            ],
            Stage.FINALISATION: [{"report": "Read execution was not replayed."}],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Read safely", "request-incomplete-read-no-retry", effort="low"
    )

    assert result.terminal_state is TerminalState.ERROR
    assert ProviderFailureCode.PROVIDER_CONNECTION_FAILED.value in result.error
    assert ProviderFailureCode.PROVIDER_CONNECTION_FAILED.value in result.text
    assert ProviderFailureCode.REPLAY_SAFETY_UNPROVEN.value in result.text
    assert result.primary_failure["code"] == (
        ProviderFailureCode.PROVIDER_CONNECTION_FAILED.value
    )
    assert result.recovery_decision["code"] == (
        ProviderFailureCode.REPLAY_SAFETY_UNPROVEN.value
    )
    assert "Possible side effects: none observed" in result.text
    assert sum(
        request.stage is Stage.EXECUTION
        for _profile, request in provider.requests
    ) == 1


@pytest.mark.asyncio
async def test_read_only_subagent_retries_once_with_same_authority(tmp_path):
    scripts = _initial("HIGH_VOLUME_TASK")
    scripts.update(
        {
            Stage.PLANNING: [
                {
                    "plan": ["delegate read-only inspection"],
                    "sub_agents": [
                        {
                            "id": "reader",
                            "task": "Inspect one source",
                            "tools": ["file_read"],
                        }
                    ],
                }
            ],
            Stage.EXECUTION: [
                StageInvocationError(
                    "reader connection reset",
                    code=ProviderFailureCode.PROVIDER_CONNECTION_FAILED,
                ),
                {"disposition": "COMPLETED", "summary": "Reader recovered."},
                {"disposition": "COMPLETED", "summary": "Primary assembled."},
            ],
            Stage.FINALISATION: [{"report": "Inspection complete."}],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Inspect in parallel", "request-readonly-subagent-retry", effort="medium"
    )

    assert result.terminal_state is TerminalState.COMPLETED
    sub_requests = [
        request
        for _profile, request in provider.requests
        if request.role == "sub_agent:reader"
    ]
    assert [request.attempt for request in sub_requests] == [1, 2]
    assert all(request.allow_side_effects is False for request in sub_requests)
    assert len({request.retry_invariant_hash for request in sub_requests}) == 1


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
    scripts[Stage.FINALISATION] = [
        {
            "execution_result": {
                "disposition": "USER_INPUT_REQUIRED",
                "summary": "Two accounts match the supplied name.",
                "evidence_refs": ["lookup:accounts"],
                "clarification": "Which account should be changed?",
            },
            "final_message": "Which account should be changed?",
        }
    ]
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Change the account", "request-execution-user-input", effort="low"
    )

    assert result.terminal_state is TerminalState.PENDING_USER_INPUT
    assert result.text == "Which account should be changed?"
    assert result.evidence_refs == ("lookup:accounts",)
    assert result.ledger["terminal_reason"] == "user_input_required"
    assert not any(
        request.stage is Stage.REVIEW
        for _profile, request in provider.requests
    )
    assert sum(
        request.stage is Stage.FINALISATION
        for _profile, request in provider.requests
    ) == 1


@pytest.mark.asyncio
async def test_execution_discovered_clarification_uses_combined_finalisation(tmp_path):
    scripts = _initial("SIMPLE_TASK")
    scripts[Stage.EXECUTION] = [
        {
            "disposition": "NEEDS_USER_INPUT",
            "summary": "Two accounts match.",
            "clarification": "Which account should be changed?",
        }
    ]
    scripts[Stage.FINALISATION] = [
        {
            "execution_result": {
                "disposition": "USER_INPUT_REQUIRED",
                "summary": "Two accounts match.",
                "clarification": "Which account should be changed?",
            },
            "final_message": (
                "Persona clarification:\n\nWhich account should be changed?"
            ),
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
    assert renderer.messages == []
    assert result.delivery_records[-1].kind == "clarification"
    assert result.delivery_records[-1].text == result.text
    assert result.ledger["terminal_reason"] == "user_input_required"


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
                    "disposition": "COMPLETED",
                    "summary": "The lightweight route produced an unverified candidate.",
                },
                {"disposition": "COMPLETED", "summary": "Verified and completed."},
            ],
            Stage.REVIEW: [
                {
                    "outcome": "FAIL",
                    "summary": "Premium capability is required for verification.",
                    "findings": ["Verify with the capable route."],
                },
                {"outcome": "PASS", "summary": "The capable route is verified."},
            ],
            Stage.REPLANNING: [{"plan": ["use the capable verification route"]}],
            Stage.FINALISATION: [{"report": "Verified and completed."}],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Complete the simple request", "request-simple-escalation", effort="xhigh"
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
    assert set(second_execution.context) == {"active_plan", "sub_agent_results"}


@pytest.mark.asyncio
async def test_malformed_execution_is_normalised_by_single_finalisation_call(
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
            Stage.FINALISATION: [
                {
                    "execution_result": {
                        "disposition": "COMPLETED",
                        "summary": "The requested change completed.",
                        "evidence_refs": ["hashi-tools:receipt-1"],
                    },
                    "final_message": "The requested change completed.",
                }
            ],
        }
    )
    provider = ScriptedProvider(scripts)
    runtime = _runtime(tmp_path, provider)

    result = await runtime.run_turn(
        "Make the requested change once",
        "request-execution-plan-b-normalisation",
        effort="low",
    )

    assert result.terminal_state is TerminalState.COMPLETED
    execution_requests = [
        request
        for _profile, request in provider.requests
        if request.stage is Stage.EXECUTION
    ]
    finalisation_requests = [
        request
        for _profile, request in provider.requests
        if request.stage is Stage.FINALISATION
    ]
    assert len(execution_requests) == 1
    assert len(finalisation_requests) == 1
    assert execution_requests[0].allow_side_effects is True
    finalisation_context = finalisation_requests[0].context
    assert finalisation_context["execution_json_valid"] is False
    assert finalisation_context["parsed_execution_result"] is None
    assert finalisation_context["raw_execution_output"]["text"].startswith(
        "The requested change completed"
    )
    assert finalisation_context["raw_execution_output"]["evidence_refs"] == [
        "hashi-tools:receipt-1"
    ]

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
    assert completed["payload"]["validation_source"] == "deferred_to_finalisation"
    assert any(
        row["event"] == "execution_structure_deferred_to_finalisation"
        for row in rows
    )


@pytest.mark.asyncio
async def test_unusable_execution_reaches_finalisation_then_runtime_error(
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
            Stage.FINALISATION: [
                {
                    "execution_result": None,
                    "final_message": (
                        "The Execution output could not be interpreted reliably."
                    ),
                }
            ],
        }
    )
    provider = ScriptedProvider(scripts)

    result = await _runtime(tmp_path, provider).run_turn(
        "Perform the external action once",
        "request-execution-result-unusable",
        effort="low",
    )

    assert result.terminal_state is TerminalState.ERROR
    assert result.ledger["status"] == "ERROR"
    assert result.ledger["terminal_reason"] == "technical_failure"
    assert result.evidence_refs == ("hashi-tools:uncertain-1",)
    assert result.error == "execution_result_unusable"
    assert result.text == "The Execution output could not be interpreted reliably."
    assert sum(
        request.stage is Stage.EXECUTION
        for _profile, request in provider.requests
    ) == 1
    assert sum(
        request.stage is Stage.FINALISATION
        for _profile, request in provider.requests
    ) == 1

    rows = [
        json.loads(line)
        for line in (tmp_path / "her-v2" / "audit.jsonl").read_text().splitlines()
    ]
    assert any(
        row["event"] == "execution_structure_deferred_to_finalisation"
        for row in rows
    )


@pytest.mark.asyncio
async def test_finalisation_cannot_change_valid_execution_disposition(tmp_path):
    scripts = _initial("SIMPLE_TASK")
    scripts.update(
        {
            Stage.EXECUTION: [
                {
                    "disposition": "COMPLETED",
                    "summary": "The requested work completed and was verified.",
                    "evidence_refs": ["receipt:complete"],
                }
            ],
            Stage.FINALISATION: [
                {
                    "execution_result": {
                        "disposition": "FAILED",
                        "summary": "Incorrect attempted override.",
                    },
                    "final_message": "The requested work completed and was verified.",
                }
            ],
        }
    )

    result = await _runtime(tmp_path, ScriptedProvider(scripts)).run_turn(
        "Complete the requested work", "request-finalisation-cannot-fail", effort="low"
    )

    assert result.terminal_state is TerminalState.COMPLETED
    assert result.ledger["status"] == "COMPLETED"
    rows = [
        json.loads(line)
        for line in (tmp_path / "her-v2" / "audit.jsonl").read_text().splitlines()
    ]
    ignored = next(
        row
        for row in rows
        if row["event"] == "finalisation_disposition_override_ignored"
    )
    assert ignored["payload"] == {
        "execution_disposition": "COMPLETED",
        "finalisation_disposition": "FAILED",
    }


@pytest.mark.asyncio
async def test_structured_output_repair_has_no_attempt_cap_and_preserves_classification(
    tmp_path,
):
    scripts = _initial("SIMPLE_TASK")
    scripts[Stage.TRIAGE] = [
        StageResponse(text="not json"),
        StageResponse(text="still not json"),
        StageResponse(text="not json on the third attempt"),
        StageResponse(text="not json on the fourth attempt"),
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
    config = _config()
    provider = ScriptedProvider(scripts)
    runtime = _runtime(tmp_path, provider, config=config)

    async def _no_delay(*_args, **_kwargs):
        await asyncio.sleep(0)

    runtime._wait_for_stage_retry = _no_delay
    result = await runtime.run_turn(
        "Do it", "request-repair", effort=Effort.LOW
    )

    assert result.classification is TriageClassification.SIMPLE_TASK
    triage_requests = [
        call for _profile, call in provider.requests if call.stage is Stage.TRIAGE
    ]
    assert len(triage_requests) == 5
    assert triage_requests[1].context["previous_structure_error"]["attempt"] == 1
    assert "no valid JSON" in triage_requests[1].context[
        "previous_structure_error"
    ]["error"]


@pytest.mark.asyncio
async def test_stop_cancels_active_execution_and_records_stopped(tmp_path):
    execution_started = asyncio.Event()

    async def blocking_execution(request):
        execution_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            request.provider_activity_callback(
                {
                    "kind": "tool_end",
                    "content": "bash cancelled after cleanup",
                    "tool_name": "bash",
                    "tool_read_only": False,
                    "tool_details": {
                        "foreground_cleanup": {
                            "status": "terminated",
                            "scope": "process_group",
                            "process_reaped": True,
                            "group_alive": False,
                            "errors": [],
                        }
                    },
                }
            )
            raise

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
    assert result.foreground_cleanup["status"] == "terminated"
    assert result.foreground_cleanup["process_reaped"] is True


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
