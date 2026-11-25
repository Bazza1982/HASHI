"""Unit tests for HAL dispatch adapters (instance identity + command results).

Focused on H2 (never drop the @instance suffix to hit a same-named agent on a
different instance) and H3 (None / unsupported / ok:false / exception are never
reported as success; accepted != completed).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from hal.dispatch import GatewayDispatchAdapter, resolve_agent_target, split_agent_spec


def test_split_agent_spec_parses_instance():
    assert split_agent_spec("@lily@hashi4") == ("lily", "hashi4")
    assert split_agent_spec("@lily") == ("lily", None)
    assert split_agent_spec("lily") == ("lily", None)
    assert split_agent_spec(None) == (None, None)


def test_resolve_agent_target_never_drops_instance_suffix():
    assert resolve_agent_target("@lily@hashi4", "hashi4") == ("lily", None)
    name, error = resolve_agent_target("@lily@hashi4", "hashi3")
    assert name is None
    assert "not this instance" in error
    assert resolve_agent_target("@lily", "hashi4") == ("lily", None)
    assert resolve_agent_target(None, "hashi4") == (None, "empty agent target")


class _FakeServer:
    def __init__(self):
        self._map = {"lily": SimpleNamespace(name="lily")}

    def _runtime_map(self):
        return self._map


def _run(coro):
    return asyncio.run(coro)


def _patch_command_exec(monkeypatch, result):
    async def fake_execute(runtime, text, **kwargs):
        return result

    import orchestrator.admin_local_testing as admin

    monkeypatch.setattr(admin, "try_execute_slash_command_text", fake_execute)


def test_command_ok_false_is_not_accepted(monkeypatch):
    _patch_command_exec(monkeypatch, {"ok": False, "error": "command disabled"})
    adapter = GatewayDispatchAdapter(_FakeServer())
    result = _run(adapter.dispatch_command(command_text="/reboot max", trigger_agent="lily"))
    assert result.accepted is False
    assert "command rejected" in result.detail


def test_command_none_is_not_supported(monkeypatch):
    _patch_command_exec(monkeypatch, None)
    adapter = GatewayDispatchAdapter(_FakeServer())
    result = _run(adapter.dispatch_command(command_text="/unknown", trigger_agent=None))
    assert result.accepted is False
    assert "not supported" in result.detail


def test_command_ok_true_marks_accepted_not_completed(monkeypatch):
    _patch_command_exec(monkeypatch, {"ok": True, "result": "scheduled"})
    adapter = GatewayDispatchAdapter(_FakeServer())
    result = _run(adapter.dispatch_command(command_text="/reboot max", trigger_agent="lily"))
    assert result.accepted is True
    assert "accepted != completed" in result.detail


class _RaisingRuntime:
    def __init__(self, exc):
        self._exc = exc

    async def enqueue_api_text(self, *args, **kwargs):
        raise self._exc


def _raising_server(exc):
    server = _FakeServer()
    server._map = {"lily": _RaisingRuntime(exc)}
    return server


def _dispatch_prompt(adapter):
    return _run(
        adapter.dispatch_prompt(
            target_agent="lily",
            text="hi",
            idempotency_key="hal:exec:0",
            exec_id="exec-1",
            stage=0,
        )
    )


def test_prompt_argument_error_is_not_retried_and_keeps_the_real_error():
    adapter = GatewayDispatchAdapter(_raising_server(TypeError("text must be str")))
    result = _dispatch_prompt(adapter)
    assert result.accepted is False
    assert result.transient is False, "a type error will never clear up on retry"
    assert "non-retryable" in result.detail
    assert "TypeError" in result.detail
    assert "text must be str" in result.detail
    assert result.evidence == {"exception_type": "TypeError", "retryable": False}


def test_prompt_transport_error_stays_retryable():
    adapter = GatewayDispatchAdapter(_raising_server(ConnectionResetError("peer reset")))
    result = _dispatch_prompt(adapter)
    assert result.accepted is False
    assert result.transient is True
    assert "ConnectionResetError" in result.detail
    assert result.evidence == {"exception_type": "ConnectionResetError", "retryable": True}


def test_prompt_permission_error_is_not_retried():
    adapter = GatewayDispatchAdapter(_raising_server(PermissionError("denied")))
    result = _dispatch_prompt(adapter)
    assert result.transient is False
    assert "PermissionError" in result.detail


def test_prompt_worker_restart_stays_retryable():
    from orchestrator.function_worker_protocol import FunctionWorkerDisconnected

    adapter = GatewayDispatchAdapter(
        _raising_server(FunctionWorkerDisconnected("lily worker is disconnected"))
    )
    result = _dispatch_prompt(adapter)
    assert result.accepted is False
    assert result.transient is True, "a restarting Worker must keep the retry budget"
    assert "FunctionWorkerDisconnected" in result.detail


def test_remote_worker_error_is_classified_by_its_reported_type():
    from hal.dispatch import classify_dispatch_error
    from orchestrator.function_worker_protocol import (
        FunctionWorkerDisconnected,
        FunctionWorkerRemoteError,
    )

    assert classify_dispatch_error(
        FunctionWorkerDisconnected("worker exited")
    ) == (True, "FunctionWorkerDisconnected")
    assert classify_dispatch_error(
        FunctionWorkerRemoteError(
            "prompt", {"type": "TimeoutError", "message": "worker still busy"}
        )
    ) == (True, "FunctionWorkerRemoteError:TimeoutError")
    assert classify_dispatch_error(
        FunctionWorkerRemoteError(
            "prompt", {"type": "ValueError", "message": "text must be str"}
        )
    ) == (False, "FunctionWorkerRemoteError:ValueError")


def test_classify_dispatch_error_defaults_to_permanent():
    from hal.dispatch import classify_dispatch_error, summarize_error

    assert classify_dispatch_error(ValueError("bad payload")) == (False, "ValueError")
    assert classify_dispatch_error(RuntimeError("queue is full")) == (True, "RuntimeError")
    assert classify_dispatch_error(asyncio.TimeoutError()) == (True, "TimeoutError")
    assert summarize_error(ValueError("a" * 400)).endswith("…")
# HASHI-Fingerprint: hfp_41e29b3efc0945b29b6e0130f9ce12f3
