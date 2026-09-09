from __future__ import annotations

import hashlib
import wave
from io import StringIO
from pathlib import Path

from rich.console import Console

from tui import sounds
from tui.app import HASHITuiApp, ChatHistory, FooterInfoBox, LogPanel, chat_message_renderable


def _render_plain(renderable) -> str:
    output = StringIO()
    Console(
        file=output,
        width=72,
        color_system=None,
        force_terminal=False,
    ).print(renderable)
    return output.getvalue()


def test_assistant_chat_message_renders_markdown_blocks():
    output = _render_plain(
        chat_message_renderable(
            "assistant",
            "智能体",
            "## Heading\n\n- first\n- second\n\n```python\nprint('ok')\n```",
        )
    )

    assert "智能体:" in output
    assert "Heading" in output
    assert "first" in output
    assert "second" in output
    assert "print('ok')" in output
    assert "## Heading" not in output
    assert "```" not in output


def test_user_chat_message_keeps_rich_markup_literal():
    output = _render_plain(
        chat_message_renderable("user", "You", "[bold red]literal[/]")
    )

    assert "You:" in output
    assert "[bold red]literal[/]" in output


def test_message_sounds_use_distinct_soft_chat_files(monkeypatch):
    played: list[Path] = []
    monkeypatch.setattr(sounds.sys, "platform", "win32")
    monkeypatch.setattr(sounds, "_play_windows_file", played.append)

    assert sounds.play_message_sound("sent") is True
    assert sounds.play_message_sound("received") is True
    assert [path.name for path in played] == [
        "soft_chat_send.wav",
        "soft_chat_receive.wav",
    ]


def test_soft_chat_assets_are_small_distinct_pcm_waves():
    properties = {}
    digests = set()
    for event, path in sounds.MESSAGE_SOUND_FILES.items():
        payload = path.read_bytes()
        digests.add(hashlib.sha256(payload).hexdigest())
        assert len(payload) < 20_000
        with wave.open(str(path), "rb") as audio:
            properties[event] = {
                "channels": audio.getnchannels(),
                "sample_width": audio.getsampwidth(),
                "sample_rate": audio.getframerate(),
                "duration": audio.getnframes() / audio.getframerate(),
            }

    assert len(digests) == 2
    assert properties["sent"]["channels"] == 1
    assert properties["sent"]["sample_width"] == 2
    assert properties["sent"]["sample_rate"] == 44_100
    assert 0.07 <= properties["sent"]["duration"] <= 0.10
    assert properties["received"]["channels"] == 1
    assert properties["received"]["sample_width"] == 2
    assert properties["received"]["sample_rate"] == 44_100
    assert 0.17 <= properties["received"]["duration"] <= 0.21


def test_message_sounds_can_be_disabled(monkeypatch):
    played: list[Path] = []
    monkeypatch.setattr(sounds.sys, "platform", "win32")
    monkeypatch.setattr(sounds, "_play_windows_file", played.append)
    monkeypatch.setenv("HASHI_TUI_SOUNDS", "off")

    assert sounds.play_message_sound("sent") is False
    assert played == []


async def test_tui_compact_design_has_bilingual_help_and_adjustable_layout(tmp_path):
    app = HASHITuiApp(bridge_home=tmp_path, launch_instance_id="HASHI1")
    app._schedule_startup_sequence = lambda: None

    async with app.run_test(size=(120, 40)) as pilot:
        app._handle_help_cmd("/help zh")
        await pilot.pause()
        chat = app.query_one("#chat-history", ChatHistory)
        rendered = "\n".join(line.text for line in chat.lines)
        assert "HASHI TUI 帮助" in rendered
        assert "/backend" in rendered
        assert "/to <agent|all>" in rendered

        app._handle_layout_cmd("/layout compact")
        assert app.query_one("#log-panel", LogPanel).display is False
        app._handle_layout_cmd("/layout balanced")
        assert app.query_one("#log-panel", LogPanel).display is True

        footer = app.query_one("#footer-info-box", FooterInfoBox)
        footer.update_state("Rika", "codex-cli", True, "flex", instance_id="HASHI1")
        assert "HASHI1 · Rika · codex-cli · Flex · API connected" in footer.render().plain
