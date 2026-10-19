from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from orchestrator import runtime_session
from orchestrator.frontend_command_admission import (
    FrontendCommandReservation,
    execute_telegram_command_invocation,
)
from orchestrator.session_store import IdempotencyConflict


class _Runtime:
    name = "nana"

    def __init__(self, workspace_dir):
        self.workspace_dir = workspace_dir
        self.global_config = SimpleNamespace(authorized_id=42, instance_id="HASHI1")

    def _is_command_allowed(self, command):
        return command != "disabled"


def _update(update_id: int = 9001):
    message = SimpleNamespace(
        message_id=update_id,
        text="/status",
        date=datetime.now(timezone.utc),
    )
    return SimpleNamespace(
        update_id=update_id,
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=99),
        effective_message=message,
        message=message,
        callback_query=None,
    )


async def _execute(runtime, update, handler, **overrides):
    values = {
        "command_name": "status",
        "arguments": [],
        "transport_id": update.update_id,
        "request_payload": {
            "update_id": str(update.update_id),
            "command": "status",
            "arguments": [],
        },
        "invoke_handler": handler,
        "command_known": True,
    }
    values.update(overrides)
    return await execute_telegram_command_invocation(runtime, update, **values)


@pytest.mark.asyncio
async def test_telegram_command_requires_registered_standard_fc_adapter(
    tmp_path, monkeypatch
):
    from orchestrator import frontend_compatibility

    runtime = _Runtime(tmp_path)
    update = _update()
    calls = []
    original = frontend_compatibility.get_compatibility_adapter

    def tracked(adapter_id):
        calls.append(adapter_id)
        return original(adapter_id)

    monkeypatch.setattr(
        frontend_compatibility, "get_compatibility_adapter", tracked
    )

    result = await _execute(runtime, update, lambda: _async_result(None))

    assert result["ok"] is True
    assert calls == ["telegram.native_command"]


@pytest.mark.asyncio
async def test_denied_command_persists_denied_authorization_decision(tmp_path):
    runtime = _Runtime(tmp_path)
    update = _update(9002)

    result = await _execute(
        runtime,
        update,
        None,
        command_name="missing-command",
        request_payload={
            "update_id": str(update.update_id),
            "command": "missing-command",
            "arguments": [],
        },
        command_known=False,
    )

    assert result["ok"] is False
    store = runtime_session.ensure_store(runtime)
    session = runtime_session.current_session_for_update(runtime, update)
    events = store.events(
        session["session_id"], owner_id=runtime_session.owner_id(runtime)
    )
    event = next(row for row in events if row["kind"] == "frontend.command_result")
    assert event["detail"]["command_invocation"]["authorization"] == {
        "decision": "denied",
        "scope": "owner",
    }


@pytest.mark.asyncio
async def test_completed_telegram_replay_returns_saved_result_without_reexecution(
    tmp_path,
):
    runtime = _Runtime(tmp_path)
    update = _update(9003)
    calls = []

    async def handler():
        calls.append("executed")
        return {"ok": True, "value": 7}

    first = await _execute(runtime, update, handler)
    replay = await _execute(runtime, update, handler)

    assert first["ok"] is True
    assert replay["ok"] is True
    assert replay["replayed"] is True
    assert replay["result"]["value"] == 7
    assert calls == ["executed"]

    store = runtime_session.ensure_store(runtime)
    session = runtime_session.current_session_for_update(runtime, update)
    events = store.events(
        session["session_id"], owner_id=runtime_session.owner_id(runtime)
    )
    assert sum(row["kind"] == "frontend.command_result" for row in events) == 1


@pytest.mark.asyncio
async def test_pending_telegram_replay_is_outcome_unknown_without_reexecution(tmp_path):
    runtime = _Runtime(tmp_path)
    update = _update(9004)
    calls = []

    async def uncertain_handler():
        calls.append("executed")
        raise RuntimeError("handler outcome is uncertain")

    with pytest.raises(RuntimeError, match="outcome is uncertain"):
        await _execute(runtime, update, uncertain_handler)

    replay = await _execute(runtime, update, uncertain_handler)

    assert replay["ok"] is False
    assert replay["outcome_unknown"] is True
    assert replay["error_code"] == "command_outcome_unknown"
    assert calls == ["executed"]


@pytest.mark.asyncio
async def test_completion_failure_leaves_pending_and_never_reexecutes(
    tmp_path, monkeypatch
):
    runtime = _Runtime(tmp_path)
    update = _update(9005)
    calls = []

    async def handler():
        calls.append("executed")
        return {"ok": True}

    def fail_completion(self, response):
        raise RuntimeError("durable completion failed")

    monkeypatch.setattr(FrontendCommandReservation, "complete", fail_completion)

    with pytest.raises(RuntimeError, match="durable completion failed"):
        await _execute(runtime, update, handler)

    replay = await _execute(runtime, update, handler)

    assert replay["outcome_unknown"] is True
    assert calls == ["executed"]


@pytest.mark.asyncio
async def test_same_transport_identity_with_different_content_conflicts(tmp_path):
    runtime = _Runtime(tmp_path)
    update = _update(9006)

    await _execute(runtime, update, lambda: _async_result(None))

    with pytest.raises(IdempotencyConflict):
        await _execute(
            runtime,
            update,
            lambda: _async_result(None),
            command_name="help",
            request_payload={
                "update_id": str(update.update_id),
                "command": "help",
                "arguments": [],
            },
        )


async def _async_result(value):
    return value
