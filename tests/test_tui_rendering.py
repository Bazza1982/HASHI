from __future__ import annotations

from io import StringIO

from rich.console import Console

from tui import sounds
from tui.app import chat_message_renderable


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


def test_message_sounds_use_distinct_windows_aliases(monkeypatch):
    played: list[str] = []
    monkeypatch.setattr(sounds.sys, "platform", "win32")
    monkeypatch.setattr(sounds, "_play_windows_alias", played.append)

    assert sounds.play_message_sound("sent") is True
    assert sounds.play_message_sound("received") is True
    assert played == ["SystemQuestion", "SystemAsterisk"]


def test_message_sounds_can_be_disabled(monkeypatch):
    played: list[str] = []
    monkeypatch.setattr(sounds.sys, "platform", "win32")
    monkeypatch.setattr(sounds, "_play_windows_alias", played.append)
    monkeypatch.setenv("HASHI_TUI_SOUNDS", "off")

    assert sounds.play_message_sound("sent") is False
    assert played == []
