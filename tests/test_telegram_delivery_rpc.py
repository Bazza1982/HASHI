from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, RetryAfter

from orchestrator.function_worker_host import FunctionWorkerHost
from orchestrator.function_worker_supervisor import AgentRuntimeHandle
from orchestrator.telegram_delivery_errors import TelegramDeliveryError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception", "code", "retry_after"),
    [
        (BadRequest("Chat not found"), "destination_not_found", None),
        (RetryAfter(timedelta(seconds=17)), "retry_after", 17),
    ],
)
async def test_worker_send_result_preserves_typed_telegram_error(
    exception, code, retry_after
):
    async def send_text(*_args, **kwargs):
        assert kwargs["_raise_delivery_error"] is True
        raise exception

    host = object.__new__(FunctionWorkerHost)
    host.runtime = SimpleNamespace(_send_text=send_text)
    host.phase = "ACTIVE"
    host.accepting = True
    host.agent_name = "zelda"

    result = await host.handle_request(
        "runtime.send_text", {"chat_id": 123, "text": "notice", "kwargs": {}}
    )

    assert result["sent"] is False
    assert result["error"]["code"] == code
    assert result["error"]["retry_after_s"] == retry_after
    assert "Chat not found" not in result["error"]["reason"]


@pytest.mark.asyncio
async def test_runtime_proxy_reconstructs_typed_delivery_error():
    handle = object.__new__(AgentRuntimeHandle)
    handle._route = AsyncMock(
        return_value={
            "sent": False,
            "error": {
                "code": "destination_forbidden",
                "retryable": False,
                "permanent": True,
                "retry_after_s": None,
                "error_type": "Forbidden",
                "reason": "telegram_destination_rejected_permission",
            },
        }
    )

    with pytest.raises(TelegramDeliveryError) as raised:
        await handle._send_text(123, "notice")

    assert raised.value.code == "destination_forbidden"
    assert raised.value.permanent is True
    assert raised.value.retryable is False
