"""Safety UI diagnostics do not authenticate actors or disclose permission objects."""
from __future__ import annotations

import errno
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.commands import safety
from orchestrator.permissions import frontend_control
from orchestrator.permissions.enterprise_decisions import SessionDecisionDenied
from tests.test_permission_safety_command import runtime_for, update_for


def runtime_case(tmp_path):
    runtime, path = runtime_for(tmp_path, [{"name": "agent"}])
    sent = []
    update = update_for(7, sent)
    update.update_id = 101
    return runtime, path, sent, update


@pytest.mark.asyncio
async def test_permission_configuration_storage_error_has_a_safe_cause(tmp_path, monkeypatch):
    runtime, path, sent, update = runtime_case(tmp_path)
    before = path.read_bytes()

    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, "private-config-path token=private-secret")

    monkeypatch.setattr(safety, "_read_status", denied)
    await safety.safety_command(runtime, update, SimpleNamespace(args=["status"]))
    assert "safety_configuration_access_denied" in sent[0][0] and "operr_" in sent[0][0]
    assert "private-" not in sent[0][0] and path.read_bytes() == before


@pytest.mark.asyncio
async def test_unapproved_actor_does_not_get_diagnostics_or_trigger_a_read(tmp_path, monkeypatch):
    runtime, path, sent, _ = runtime_case(tmp_path)
    monkeypatch.setattr(safety, "_read_status", lambda *a: pytest.fail("Unapproved actor reached storage"))
    await safety.safety_command(runtime, update_for(8, sent), SimpleNamespace(args=["status"]))
    assert sent == [] and path.exists()


@pytest.mark.asyncio
async def test_unavailable_permission_subject_omits_object_and_actor_details(tmp_path, monkeypatch):
    runtime, _, sent, update = runtime_case(tmp_path)
    def unavailable(*args, **kwargs):
        raise ValueError("private-other-owner private-request-id")
    monkeypatch.setattr(safety, "_subject_for_update", unavailable)
    await safety.safety_command(runtime, update, SimpleNamespace(args=["requests"]))
    assert "safety_scope_scope_unavailable" in sent[0][0] and "operr_" in sent[0][0]
    assert "private-" not in sent[0][0]


@pytest.mark.asyncio
async def test_permission_request_read_failure_is_not_an_empty_request_list(tmp_path, monkeypatch):
    runtime, _, sent, update = runtime_case(tmp_path)
    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, "private-request-database")
    authority = SimpleNamespace(list_session_requests=denied)
    monkeypatch.setattr(safety, "_subject_for_update", lambda *a: (authority, object()))
    await safety.safety_command(runtime, update, SimpleNamespace(args=["requests"]))
    assert "safety_read_access_denied" in sent[0][0] and "operr_" in sent[0][0]
    assert "private-" not in sent[0][0]


@pytest.mark.asyncio
async def test_invalid_withdraw_revision_has_an_input_category_without_an_authority_write(tmp_path, monkeypatch):
    runtime, _, sent, update = runtime_case(tmp_path)
    authority = SimpleNamespace(withdraw_session_request=lambda *a: pytest.fail("Invalid revision reached authority"))
    monkeypatch.setattr(safety, "_subject_for_update", lambda *a: (authority, object()))
    await safety.safety_command(
        runtime, update, SimpleNamespace(args=["withdraw", "private-request", "--revision", "0"]),
    )
    assert "safety_validate_invalid_input" in sent[0][0] and "operr_" in sent[0][0]
    assert "private-request" not in sent[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["decision", "management", "callback"])
@pytest.mark.parametrize("rejected", [False, True])
async def test_permission_control_failure_preserves_unknown_vs_authority_rejection(
    tmp_path, monkeypatch, kind, rejected,
):
    runtime, _, sent, update = runtime_case(tmp_path)
    runtime.permission_authority = object()
    monkeypatch.setattr(safety, "_subject_for_update", lambda *a: (runtime.permission_authority, object()))
    calls = []
    def failed(*args, **kwargs):
        calls.append(True)
        if rejected:
            raise SessionDecisionDenied("private-owner-and-token")
        raise OSError(errno.EIO, "private-after-submit")
    async def manage(*args, **kwargs):
        return failed(*args, **kwargs)
    ingress = SimpleNamespace(decide=failed, submit_action=failed, manage=manage)
    monkeypatch.setattr(frontend_control, "current_permission_ingress", lambda: ingress)
    operation = "management" if kind == "management" else "decision"
    if kind == "callback":
        update.callback_query = SimpleNamespace(data="safety:d:" + "a" * 32, answer=AsyncMock())
        result = await safety.safety_decision_callback(runtime, update, None)
    else:
        args = ["revoke", "private-task"] if kind == "management" else ["deny", "private-request", "--revision", "1"]
        result = await safety.safety_command(runtime, update, SimpleNamespace(args=args))
    message = sent[0][0]
    assert "operr_" in message and "private-" not in message
    assert result["ok"] is False
    assert result["request_outcome"] == ("rejected" if rejected else "unconfirmed")
    assert result["error_code"] == "permission_" + operation + ("_rejected" if rejected else "_unconfirmed")
    assert "safety_" + operation + ("_scope_unavailable" if rejected else "_io_error") in message
    assert calls == [True]


@pytest.mark.asyncio
async def test_permission_action_rendering_failure_is_visible_without_issuing_a_control(tmp_path, monkeypatch):
    runtime, _, sent, update = runtime_case(tmp_path)
    monkeypatch.setattr(safety, "_request_command", lambda *a: "<b>Existing authorized request</b>")
    def unavailable(*args, **kwargs):
        raise PermissionError(errno.EACCES, "private-actions")
    monkeypatch.setattr(safety, "_decision_markup", unavailable)
    await safety.safety_command(runtime, update, SimpleNamespace(args=["request", "private-request"]))
    assert "Existing authorized request" in sent[0][0]
    assert "safety_presentation_access_denied" in sent[0][0] and "operr_" in sent[0][0]
    assert "private-" not in sent[0][0] and sent[0][1].get("reply_markup") is None
