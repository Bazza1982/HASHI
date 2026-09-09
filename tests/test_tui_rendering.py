from __future__ import annotations

import hashlib
import json
import wave
from io import StringIO
from pathlib import Path

from rich.console import Console

from tui import sounds
from tui.app import (
    HASHITuiApp,
    ChatHistory,
    ChatInput,
    CommandPreview,
    FooterInfoBox,
    LogPanel,
    chat_message_renderable,
)


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


def test_message_sounds_use_wsl_pulse_audio_without_blocking(monkeypatch):
    launched = []
    monkeypatch.setattr(sounds.sys, "platform", "linux")
    monkeypatch.setattr(
        sounds.shutil,
        "which",
        lambda name: "/usr/bin/paplay" if name == "paplay" else None,
    )
    monkeypatch.setattr(
        sounds.subprocess,
        "Popen",
        lambda argv, **kwargs: launched.append((argv, kwargs)),
    )

    assert sounds.play_message_sound("sent") is True
    assert launched[0][0] == [
        "/usr/bin/paplay",
        str(sounds.MESSAGE_SOUND_FILES["sent"]),
    ]
    assert launched[0][1]["start_new_session"] is True


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


async def test_tui_language_balanced_logo_and_command_preview(tmp_path):
    app = HASHITuiApp(bridge_home=tmp_path, launch_instance_id="HASHI1")
    app._schedule_startup_sequence = lambda: None

    async with app.run_test(size=(120, 40)) as pilot:
        app._handle_tui_cmd("/tui language zh")
        await pilot.pause()
        assert app._ui_language == "zh"
        assert "主机日志" in app.query_one("#log-panel", LogPanel).border_title
        assert "输入消息" in app.query_one("#chat-input", ChatInput).placeholder
        assert "API 离线" in app.query_one("#footer-info-box", FooterInfoBox).render().plain

        app._handle_layout_cmd("/layout balanced")
        await pilot.pause()
        log_text = "\n".join(line.text for line in app.query_one("#log-panel", LogPanel).lines)
        assert "██╗" in log_text

        input_box = app.query_one("#chat-input", ChatInput)
        input_box.focus()
        await pilot.press("/")
        await pilot.pause()
        preview = app.query_one("#command-preview", CommandPreview)
        assert preview.display is True
        assert app._command_matches == ["/help", "/to", "/mode", "/model", "/backend"]
        await pilot.press("down")
        assert app._current_command_match == "/to"
        await pilot.press("tab")
        assert input_box.value == "/to"

        input_box.value = "/he"
        await pilot.pause()
        assert app._current_command_match == "/help"
        await pilot.press("tab")
        assert input_box.value == "/help"

    preferences = json.loads((tmp_path / "state" / "tui_preferences.json").read_text())
    assert preferences == {"language": "zh", "layout": "balanced", "sounds": True}


async def test_tui_enter_completes_prefix_and_rejects_unknown_slash_command(tmp_path):
    app = HASHITuiApp(bridge_home=tmp_path, launch_instance_id="HASHI1")
    app._schedule_startup_sequence = lambda: None
    sent: list[str] = []
    app._send_message = lambda text, *_args: sent.append(text)
    app._play_message_sound = lambda _event: True

    async with app.run_test(size=(120, 40)) as pilot:
        app.gateway_ok = True
        app.current_agent = "rika"
        app.current_agent_display = "Rika"
        input_box = app.query_one("#chat-input", ChatInput)
        input_box.focus()

        input_box.value = "/mod"
        await pilot.pause()
        assert app._current_command_match == "/mode"
        await pilot.press("enter")
        assert sent == ["/mode"]

        input_box.value = "/effortt"
        await pilot.press("enter")
        assert sent == ["/mode"]
        chat = app.query_one("#chat-history", ChatHistory)
        rendered = "\n".join(line.text for line in chat.lines)
        assert "Unknown command: /effortt" in rendered

        # Exact non-menu commands remain valid even though they are not previewed.
        input_box.value = "/move"
        await pilot.press("enter")
        assert sent == ["/mode", "/move"]


async def test_tui_sound_setting_is_persisted_and_can_be_previewed(tmp_path):
    app = HASHITuiApp(bridge_home=tmp_path, launch_instance_id="HASHI1")
    app._schedule_startup_sequence = lambda: None
    played: list[str] = []
    app._play_message_sound = lambda event: played.append(event) or True

    async with app.run_test(size=(120, 40)):
        app._handle_tui_cmd("/tui sound off")
        assert app._sound_enabled is False
        app._handle_tui_cmd("/tui sound on")
        assert app._sound_enabled is True
        assert played == ["received"]

    preferences = json.loads((tmp_path / "state" / "tui_preferences.json").read_text())
    assert preferences["sounds"] is True
