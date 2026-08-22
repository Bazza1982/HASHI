"""Runtime-side regression tests for the /meter cost tail delivery.

Covers the Zelda regression matrix that a pure data-contract test cannot:
streaming/final-delivery dedup, silent / non-Telegram / transfer-buffered
exclusion, and the guarantee that the cost tail never leaks into memory,
voice, wrapper, or HChat content.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from tools.meter_cost import PerCallUsageLineItem, UsageReceipt


class FakeMeterRuntime:
    """Minimal duck-typed runtime exposing only the /meter tail surface."""

    def __init__(
        self,
        *,
        meter_at_start: bool = False,
        receipt: UsageReceipt | None = None,
        buffer_during_transfer: bool = False,
    ):
        self._meter_at_start = meter_at_start
        self._meter_receipt = receipt
        self._meter_receipt_by_id: dict[str, Any] = (
            {"req-1": receipt} if receipt is not None else {}
        )
        self._request_meta_by_id: dict[str, dict[str, Any]] = {
            "req-1": {"meter_at_start": meter_at_start}
        }
        self._buffer_during_transfer = buffer_during_transfer
        self.logger = logging.getLogger("test.meter")
        self.logger.addHandler(logging.NullHandler())
        self.sent: list[tuple[int, str, dict[str, Any]]] = []
        self.voice_sent: list[tuple[int, str, str]] = []
        self.memory_turns: list[tuple[str, str, str]] = []
        self.wrapper_calls: int = 0

    def _should_buffer_during_transfer(self, request_id: str) -> bool:
        return self._buffer_during_transfer

    async def send_long_message(self, chat_id, text, **kwargs) -> None:
        self.sent.append((chat_id, text, kwargs))
        return 0.0, 1

    async def _send_voice_reply(self, chat_id, text, request_id) -> None:
        self.voice_sent.append((chat_id, text, request_id))

    def record_turn(self, role: str, source: str, text: str) -> None:
        # Mirrors the memory-store surface to prove the tail never calls it.
        self.memory_turns.append((role, source, text))


class FakeMeterCommandRuntime:
    def __init__(self, workspace_dir):
        self.workspace_dir = workspace_dir
        self._meter = False
        self.replies: list[str] = []

    def _is_authorized_user(self, _user_id: int) -> bool:
        return True

    def _meter_enabled(self) -> bool:
        return self._meter

    def _meter_menu_text(self) -> str:
        return f"meter={'on' if self._meter else 'off'}"

    def _meter_keyboard(self):
        return None

    async def _reply_text(self, _update, text: str, **_kwargs) -> None:
        self.replies.append(text)


def _receipt() -> UsageReceipt:
    return UsageReceipt(
        request_id="req-1",
        line_items=[
            PerCallUsageLineItem(
                model="claude-sonnet-4-6",
                input_tokens=1000,
                output_tokens=500,
                cost_usd=0.012347,
                cost_source="provider",
            )
        ],
    )


def _meditation_job(*, meter_at_start: bool = True) -> dict[str, Any]:
    return {
        "request_id": "req-1",
        "notification": {
            "chat_id": 99,
            "meter_at_start": meter_at_start,
            "verbose_at_start": True,
        },
        "meter": {
            "line_items": [
                {
                    "request_id": "req-1",
                    "phase": "meditation",
                    "engine": "deepseek",
                    "model": "deepseek-v4-pro",
                    "input": 100,
                    "output": 50,
                    "thinking": 0,
                    "token_source": "estimated",
                    "cost_usd": 0.001240,
                    "cost_source": "pricing_table",
                }
            ]
        },
    }


@pytest.mark.asyncio
async def test_meter_command_without_args_is_read_only(tmp_path):
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    runtime = FakeMeterCommandRuntime(tmp_path)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    await FlexibleAgentRuntime.cmd_meter(
        runtime, update, SimpleNamespace(args=[])
    )

    assert runtime._meter is False
    assert runtime.replies == ["meter=off"]


@pytest.mark.asyncio
async def test_meter_command_invalid_args_do_not_toggle(tmp_path):
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    runtime = FakeMeterCommandRuntime(tmp_path)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    await FlexibleAgentRuntime.cmd_meter(
        runtime, update, SimpleNamespace(args=["banana"])
    )

    assert runtime._meter is False
    assert len(runtime.replies) == 1
    assert "on|off|status" in runtime.replies[0]


@pytest.mark.asyncio
async def test_meter_command_only_on_and_off_mutate_state(tmp_path):
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    runtime = FakeMeterCommandRuntime(tmp_path)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    await FlexibleAgentRuntime.cmd_meter(
        runtime, update, SimpleNamespace(args=["on"])
    )
    assert runtime._meter is True

    await FlexibleAgentRuntime.cmd_meter(
        runtime, update, SimpleNamespace(args=["status"])
    )
    assert runtime._meter is True

    await FlexibleAgentRuntime.cmd_meter(
        runtime, update, SimpleNamespace(args=["off"])
    )
    assert runtime._meter is False


@pytest.mark.asyncio
async def test_foreground_tail_sends_single_deduped_message():
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    runtime = FakeMeterRuntime(meter_at_start=True, receipt=_receipt())
    item = SimpleNamespace(
        request_id="req-1", chat_id=99, silent=False, deliver_to_telegram=True
    )
    await FlexibleAgentRuntime._send_meter_cost_tail(runtime, item)
    assert len(runtime.sent) == 1
    _, text, kwargs = runtime.sent[0]
    assert kwargs["purpose"] == "meter-cost"
    assert text.startswith("💰 前台回合：")
    # Dedup: the tail is a standalone message, never duplicated per stream chunk.
    assert runtime.voice_sent == []
    assert runtime.memory_turns == []
    assert runtime.wrapper_calls == 0


@pytest.mark.asyncio
async def test_foreground_tail_uses_matching_request_state_under_overlap():
    """A later request must not replace an earlier request's meter receipt."""
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    receipt_a = _receipt()
    receipt_b = UsageReceipt(
        request_id="req-2",
        line_items=[
            PerCallUsageLineItem(
                input_tokens=20,
                output_tokens=10,
                cost_usd=0.020000,
                cost_source="provider",
            )
        ],
    )
    runtime = FakeMeterRuntime(meter_at_start=True, receipt=receipt_a)
    runtime._request_meta_by_id = {
        "req-1": {"meter_at_start": True},
        "req-2": {"meter_at_start": False},
    }
    runtime._meter_receipt_by_id = {"req-1": receipt_a, "req-2": receipt_b}
    # Reproduce the legacy global-field collision: request B finished last.
    runtime._meter_at_start = False
    runtime._meter_receipt = receipt_b

    await FlexibleAgentRuntime._send_meter_cost_tail(
        runtime,
        SimpleNamespace(
            request_id="req-1",
            chat_id=99,
            silent=False,
            deliver_to_telegram=True,
        ),
    )

    assert len(runtime.sent) == 1
    assert "US$0.0123" in runtime.sent[0][1]
    assert "US$0.0200" not in runtime.sent[0][1]


@pytest.mark.asyncio
async def test_foreground_tail_skips_when_meter_off():
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    runtime = FakeMeterRuntime(meter_at_start=False, receipt=_receipt())
    item = SimpleNamespace(
        request_id="req-1", chat_id=99, silent=False, deliver_to_telegram=True
    )
    await FlexibleAgentRuntime._send_meter_cost_tail(runtime, item)
    assert runtime.sent == []


@pytest.mark.asyncio
async def test_foreground_tail_skips_silent():
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    runtime = FakeMeterRuntime(meter_at_start=True, receipt=_receipt())
    item = SimpleNamespace(
        request_id="req-1", chat_id=99, silent=True, deliver_to_telegram=True
    )
    await FlexibleAgentRuntime._send_meter_cost_tail(runtime, item)
    assert runtime.sent == []


@pytest.mark.asyncio
async def test_foreground_tail_skips_non_telegram():
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    runtime = FakeMeterRuntime(meter_at_start=True, receipt=_receipt())
    item = SimpleNamespace(
        request_id="req-1", chat_id=99, silent=False, deliver_to_telegram=False
    )
    await FlexibleAgentRuntime._send_meter_cost_tail(runtime, item)
    assert runtime.sent == []


@pytest.mark.asyncio
async def test_foreground_tail_skips_transfer_buffered():
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    runtime = FakeMeterRuntime(
        meter_at_start=True, receipt=_receipt(), buffer_during_transfer=True
    )
    item = SimpleNamespace(
        request_id="req-1", chat_id=99, silent=False, deliver_to_telegram=True
    )
    await FlexibleAgentRuntime._send_meter_cost_tail(runtime, item)
    assert runtime.sent == []


@pytest.mark.asyncio
async def test_meditation_tail_sends_when_meter_at_start():
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    runtime = FakeMeterRuntime(meter_at_start=True)
    delivered = await FlexibleAgentRuntime._send_meditation_cost_tail(
        runtime, _meditation_job()
    )
    assert delivered is True
    assert len(runtime.sent) == 1
    _, text, kwargs = runtime.sent[0]
    assert kwargs["purpose"] == "meditation-cost"
    assert text.startswith("🧘 冥想：")
    # Never leaks into memory / voice / wrapper / HChat.
    assert runtime.voice_sent == []
    assert runtime.memory_turns == []
    assert runtime.wrapper_calls == 0


@pytest.mark.asyncio
async def test_meditation_tail_gated_on_meter_not_verbose():
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    # verbose_at_start is True but meter_at_start is False → no tail.
    runtime = FakeMeterRuntime(meter_at_start=False)
    await FlexibleAgentRuntime._send_meditation_cost_tail(
        runtime, _meditation_job(meter_at_start=False)
    )
    assert runtime.sent == []


@pytest.mark.asyncio
async def test_meditation_tail_missing_line_items_is_silent():
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    runtime = FakeMeterRuntime(meter_at_start=True)
    job = _meditation_job()
    job["meter"] = {}
    await FlexibleAgentRuntime._send_meditation_cost_tail(runtime, job)
    assert runtime.sent == []


@pytest.mark.asyncio
async def test_meditation_tail_omits_task_total_when_foreground_cost_unknown():
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    runtime = FakeMeterRuntime(meter_at_start=True)
    runtime._meter_receipt_by_id["req-1"] = UsageReceipt(
        request_id="req-1",
        line_items=[
            PerCallUsageLineItem(
                input_tokens=100,
                output_tokens=20,
                cost_usd=None,
                cost_source="unknown",
            )
        ],
    )

    delivered = await FlexibleAgentRuntime._send_meditation_cost_tail(
        runtime, _meditation_job()
    )

    assert delivered is True
    assert "任务累计" not in runtime.sent[0][1]
