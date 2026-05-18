from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from orchestrator.ephemeral_invoker import BackendSidecarInvoker, SidecarFailureResponse


@dataclass
class _BackendResponse:
    text: str
    is_success: bool = True
    usage: object | None = None
    cost_usd: float | None = None


class _BackendManager:
    def __init__(self, *, fail: bool = False, workspace_dir=None):
        self.config = SimpleNamespace(
            active_backend="codex-cli",
            workspace_dir=workspace_dir,
            allowed_backends=[
                {"engine": "codex-cli", "model": "gpt-5.4"},
                {"engine": "claude-cli", "model": "claude-sonnet"},
            ],
        )
        self.current_backend = SimpleNamespace(config=SimpleNamespace(model="gpt-5.5"))
        self.fail = fail
        self.ephemeral_calls = []
        self.core_calls = []
        self.audit_backend = "claude-cli"

    async def generate_ephemeral_response(self, **kwargs):
        self.ephemeral_calls.append(kwargs)
        if self.fail:
            raise RuntimeError("backend unavailable")
        return _BackendResponse("sidecar response")

    async def generate_response(self, *args, **kwargs):
        self.core_calls.append((args, kwargs))
        return _BackendResponse("core response")


def test_sidecar_context_uses_active_core_backend_and_current_model():
    manager = _BackendManager()
    invoker = BackendSidecarInvoker(manager, session_id_getter=lambda: "session-test")

    assert invoker.current_context() == {
        "engine": "codex-cli",
        "model": "gpt-5.5",
    }


@pytest.mark.asyncio
async def test_sidecar_invocation_uses_ephemeral_backend_without_core_mutation():
    manager = _BackendManager()
    invoker = BackendSidecarInvoker(manager, session_id_getter=lambda: "session-test")

    response = await invoker(
        engine="codex-cli",
        model="gpt-5.5",
        prompt="Classify this turn.",
        request_id="req-1",
        silent=True,
    )

    assert response.is_success is True
    assert response.text == "sidecar response"
    assert manager.ephemeral_calls == [
        {
            "engine": "codex-cli",
            "model": "gpt-5.5",
            "prompt": "Classify this turn.",
            "request_id": "req-1",
            "silent": True,
        }
    ]
    assert manager.core_calls == []


@pytest.mark.asyncio
async def test_sidecar_invocation_failure_returns_controlled_response(caplog):
    manager = _BackendManager(fail=True)
    invoker = BackendSidecarInvoker(manager)

    response = await invoker(
        engine="codex-cli",
        model="gpt-5.5",
        prompt="Classify this turn.",
        request_id="req-fail",
    )

    assert isinstance(response, SidecarFailureResponse)
    assert response.is_success is False
    assert response.text == ""
    assert response.error == "backend unavailable"
    assert "Sidecar invocation failed" in caplog.text


def test_sidecar_context_does_not_use_audit_backend_config():
    manager = _BackendManager()
    manager.audit_backend = "claude-cli"
    invoker = BackendSidecarInvoker(manager)

    context = invoker.current_context()

    assert context == {
        "engine": "codex-cli",
        "model": "gpt-5.5",
    }
    assert context["engine"] != manager.audit_backend


def test_sidecar_context_falls_back_to_allowed_backend_model():
    manager = _BackendManager()
    manager.current_backend = None
    invoker = BackendSidecarInvoker(manager)

    assert invoker.current_context() == {
        "engine": "codex-cli",
        "model": "gpt-5.4",
    }


@pytest.mark.asyncio
async def test_sidecar_invocation_records_usage_and_audit(tmp_path):
    manager = _BackendManager(workspace_dir=tmp_path)
    manager.response = _BackendResponse(
        "sidecar response",
        usage=SimpleNamespace(input_tokens=11, output_tokens=7, thinking_tokens=3),
        cost_usd=0.001,
    )

    async def generate_ephemeral_response(**kwargs):
        manager.ephemeral_calls.append(kwargs)
        return manager.response

    manager.generate_ephemeral_response = generate_ephemeral_response
    invoker = BackendSidecarInvoker(manager, session_id_getter=lambda: "session-test")

    await invoker(
        engine="codex-cli",
        model="gpt-5.5",
        prompt="Classify this turn.",
        request_id="req-usage",
    )

    usage = json.loads((tmp_path / "token_usage.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    audit = json.loads((tmp_path / "token_audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])

    assert usage["session_id"] == "session-test"
    assert usage["input"] == 11
    assert usage["output"] == 7
    assert usage["thinking"] == 3
    assert audit["completion_path"] == "sidecar"
    assert audit["request_id"] == "req-usage"
