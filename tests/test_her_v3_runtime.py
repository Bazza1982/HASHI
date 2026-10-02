"""Observable HERV3 turn boundaries after the staged HERV2 workflow retired."""
from __future__ import annotations

import asyncio

import pytest

from orchestrator.her_v2.audit import DurableAuditLog
from orchestrator.her_v2.config import HERv2Config
from orchestrator.her_v2.interfaces import RecordingDelivery
from orchestrator.her_v2.ledger import LedgerStore
from orchestrator.her_v2.models import Stage, StageResponse, TerminalState
from orchestrator.her_v2.presentation import FinalStyleResult
from orchestrator.her_v2.runtime import HERv2Runtime


class _MainProvider:
    def __init__(self, response="Main answer."):
        self.response = response
        self.calls = []
        self.started = asyncio.Event()
        self.cancelled = False

    def tool_catalogue(self, *, allow_side_effects, delegated_tools=None):
        return ()

    async def invoke(self, profile, request):
        self.calls.append((profile, request))
        self.started.set()
        try:
            value = self.response(request) if callable(self.response) else self.response
            if asyncio.iscoroutine(value):
                value = await value
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if isinstance(value, StageResponse):
            return value
        return StageResponse(
            data={"message": value},
            provider=profile.engine,
            model=profile.model,
        )


class _FailWriter:
    def append(self, _record):
        raise OSError("audit offline")


class _StyleRenderer:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.requests = []

    async def render(self, request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("style provider unavailable")
        return FinalStyleResult(
            source_event_id=request.event_id,
            decision="rewrite",
            text="Short final answer.",
            provenance="test_final_style",
        )


def _runtime(
    tmp_path, provider, *, delivery=None, audit=None, config=None, final_style=None
):
    return HERv2Runtime(
        config=config
        or HERv2Config.from_mapping(
            {"main": {"provider": "fake-api", "model": "main-model"}}
        ),
        provider=provider,
        ledger_store=LedgerStore(tmp_path / "ledgers"),
        audit_log=audit
        or DurableAuditLog(
            tmp_path / "audit.jsonl", tmp_path / "audit-fallback.jsonl"
        ),
        delivery=delivery or RecordingDelivery(),
        final_style=final_style,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["zero", "low", "medium", "high", "xhigh", "max"])
async def test_every_effort_uses_one_main_model_without_staged_calls(tmp_path, effort):
    provider = _MainProvider()
    result = await _runtime(tmp_path, provider).run_turn(
        "Answer the request", f"request-{effort}", effort=effort
    )

    assert result.terminal_state is TerminalState.COMPLETED
    assert result.text == "Main answer."
    assert [(item.kind, item.text) for item in result.delivery_records] == [
        ("final", "Main answer.")
    ]
    assert len(provider.calls) == 1
    profile, request = provider.calls[0]
    assert (profile.engine, profile.model) == ("fake-api", "main-model")
    assert request.stage is Stage.DIRECT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("style_fails", "expected"),
    [(False, "Short final answer."), (True, "Main answer.")],
)
async def test_optional_final_style_cannot_lose_the_main_answer(
    tmp_path, style_fails, expected
):
    renderer = _StyleRenderer(fail=style_fails)
    provider = _MainProvider()
    result = await _runtime(
        tmp_path, provider, final_style=renderer
    ).run_turn("Answer briefly", "request-style", effort="zero")

    assert result.terminal_state is TerminalState.COMPLETED
    assert result.text == expected
    assert [(item.kind, item.text) for item in result.delivery_records] == [
        ("final", expected)
    ]
    assert len(provider.calls) == 1
    assert len(renderer.requests) == 1
    assert renderer.requests[0].draft_text == "Main answer."


@pytest.mark.asyncio
async def test_route_activity_keeps_selected_and_returned_model_identities_distinct(
    tmp_path,
):
    release = asyncio.Event()

    async def answer(_request):
        await release.wait()
        return StageResponse(
            data={"message": "Done."},
            provider="actual-api",
            model="actual-model",
        )

    provider = _MainProvider(answer)
    delivery = RecordingDelivery()
    turn = asyncio.create_task(
        _runtime(tmp_path, provider, delivery=delivery).run_turn(
            "Answer directly", "request-model-route", effort="zero"
        )
    )
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    try:
        selected = [
            row
            for row in delivery.activity_records
            if row["kind"] == "model_route"
            and row["metadata"]["route_status"] == "selected"
        ]
        assert len(selected) == 1
        assert selected[0]["metadata"]["model_provider"] == "fake-api"
        assert selected[0]["metadata"]["model"] == "main-model"
    finally:
        release.set()
    result = await turn
    returned = [
        row
        for row in delivery.activity_records
        if row["kind"] == "model_route"
        and row["metadata"]["route_status"] == "returned"
    ]
    assert result.terminal_state is TerminalState.COMPLETED
    assert len(returned) == 1
    assert returned[0]["metadata"]["model_provider"] == "actual-api"
    assert returned[0]["metadata"]["model"] == "actual-model"


@pytest.mark.asyncio
async def test_stop_interrupts_the_main_call_before_final_delivery(tmp_path):
    async def blocking(_request):
        await asyncio.Event().wait()

    provider = _MainProvider(blocking)
    runtime = _runtime(tmp_path, provider)
    task = asyncio.create_task(
        runtime.run_turn("Long request", "request-stop", turn_id="turn-stop")
    )
    await asyncio.wait_for(provider.started.wait(), timeout=2)

    assert await runtime.stop_turn("turn-stop", reason="USER_STOP") is True
    result = await asyncio.wait_for(task, timeout=2)
    assert result.terminal_state is TerminalState.STOPPED
    assert result.ledger["terminal_reason"] == "USER_STOP"
    assert provider.cancelled is True
    assert all(item.kind != "final" for item in result.delivery_records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal", "expected"),
    [("ERROR", TerminalState.ERROR), ("STOPPED", TerminalState.STOPPED)],
)
async def test_total_audit_failure_fails_closed_before_provider(
    tmp_path, terminal, expected
):
    provider = _MainProvider()
    audit = DurableAuditLog(
        primary_writer=_FailWriter(), fallback_writer=_FailWriter()
    )
    runtime = _runtime(
        tmp_path,
        provider,
        audit=audit,
        config=HERv2Config.from_mapping(
            {
                "main": {"provider": "fake-api", "model": "main-model"},
                "audit_failure_terminal": terminal,
            }
        ),
    )
    result = await runtime.run_turn("Hello", f"request-audit-{terminal}")

    assert result.terminal_state is expected
    assert provider.calls == []
    assert all(item.kind != "final" for item in result.delivery_records)
    if terminal == "STOPPED":
        assert result.ledger["terminal_reason"].startswith(
            "AUDIT_PERSISTENCE_FAILURE"
        )
