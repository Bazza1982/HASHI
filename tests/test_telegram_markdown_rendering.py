from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.legacy.bridge_agent_runtime import BridgeAgentRuntime
from orchestrator.runtime_common import _md_to_html, _streaming_status_to_html


class _Bot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, **kwargs) -> None:
        self.sent.append(kwargs)


class _Transcript:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str, str]] = []

    def append_transcript(self, role: str, text: str, source: str) -> None:
        self.entries.append((role, text, source))


def _runtime() -> SimpleNamespace:
    bot = _Bot()
    runtime = SimpleNamespace(
        name="lily<&",
        _openrouter_think_chunk="",
        _think_buffer=["Use **Repair** and `release-proof.json` <safely>."],
        telegram_connected=True,
        _notify_enabled=True,
        app=SimpleNamespace(bot=bot),
        telegram_logger=SimpleNamespace(warning=lambda _message: None),
        handoff_builder=_Transcript(),
    )
    runtime.append_conversation_entry = runtime.handoff_builder.append_transcript
    return runtime


def test_markdown_renderer_keeps_incomplete_stream_markup_safe() -> None:
    assert _md_to_html("Working on **Repair <draft>") == (
        "Working on **Repair &lt;draft&gt;"
    )


def test_streaming_status_renders_markdown_and_escapes_identity() -> None:
    rendered = _streaming_status_to_html(
        "lily<&",
        "codex<&",
        7,
        ["💭 **Repair** `release-proof.json`"],
    )

    assert "<b>lily&lt;&amp;</b> | codex&lt;&amp; | 7s" in rendered
    assert "💭 <b>Repair</b> <code>release-proof.json</code>" in rendered
    assert "**Repair**" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flush_thinking",
    [
        pytest.param(FlexibleAgentRuntime._flush_thinking, id="flexible"),
        pytest.param(BridgeAgentRuntime._flush_thinking, id="legacy"),
    ],
)
async def test_think_delivery_renders_markdown_for_both_runtimes(flush_thinking) -> None:
    runtime = _runtime()

    await flush_thinking(runtime, 123)

    assert runtime.app.bot.sent == [
        {
            "chat_id": 123,
            "text": (
                "💭 Use <b>Repair</b> and "
                "<code>release-proof.json</code> &lt;safely&gt;."
            ),
            "parse_mode": "HTML",
            **(
                {"disable_notification": False}
                if flush_thinking is FlexibleAgentRuntime._flush_thinking
                else {}
            ),
        }
    ]
    assert "<i>" not in runtime.app.bot.sent[0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flush_thinking",
    [
        pytest.param(FlexibleAgentRuntime._flush_thinking, id="flexible"),
        pytest.param(BridgeAgentRuntime._flush_thinking, id="legacy"),
    ],
)
async def test_think_delivery_chunks_long_commentary_without_truncating(flush_thinking) -> None:
    runtime = _runtime()
    commentary = "Codex commentary\n\n" + ("full-content " * 400)
    runtime._think_buffer = [commentary]
    long_sends: list[dict] = []

    async def send_long_message(chat_id, text, **kwargs):
        long_sends.append({"chat_id": chat_id, "text": text, **kwargs})
        return 0.0, 2

    runtime.send_long_message = send_long_message

    await flush_thinking(runtime, 123)

    assert runtime.app.bot.sent == []
    assert long_sends == [
        {"chat_id": 123, "text": f"💭 {commentary}", "purpose": "think"}
    ]
    assert runtime.handoff_builder.entries[-1] == (
        "thinking",
        f"💭 {commentary}",
        "think",
    )
