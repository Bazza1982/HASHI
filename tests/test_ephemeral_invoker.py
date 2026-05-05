from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from orchestrator.ephemeral_invoker import BackendSidecarInvoker, SidecarFailureResponse


@dataclass
class _BackendResponse:
    text: str
    is_success: bool = True


class _BackendManager:
    def __init__(self, *, fail: bool = False):
        self.config = SimpleNamespace(
            active_backend="codex-cli",
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
    invoker = BackendSidecarInvoker(manager)

    assert invoker.current_context() == {
        "engine": "codex-cli",
        "model": "gpt-5.5",
    }


@pytest.mark.asyncio
async def test_sidecar_invocation_uses_ephemeral_backend_without_core_mutation():
    manager = _BackendManager()
    invoker = BackendSidecarInvoker(manager)

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
