from __future__ import annotations

import json
import sys

import pytest

from orchestrator import banner, terminal_console
from orchestrator.banner import (
    AgentBannerStatus,
    ServiceBannerStatus,
    StartupAnimationResult,
    _show_ascii_startup_banner,
    show_startup_banner,
    show_startup_header,
    show_startup_status,
)
from orchestrator.terminal_console import TerminalAnimationCapability


def test_static_startup_summary_matches_operator_contract(capsys) -> None:
    show_startup_header(
        instance_name="HASHI3",
        instance_path=r"C:\Users\thene\projects\HASHI3",
        platform_name="windows_native",
    )
    show_startup_status(
        agents=[
            AgentBannerStatus(
                name="agent1",
                state="ONLINE",
                telegram_connected=True,
            )
        ],
        services=[
            ServiceBannerStatus(
                name="Backend API",
                url="http://127.0.0.1:18804",
            ),
            ServiceBannerStatus(
                name="API Gateway",
                url="http://127.0.0.1:18805",
            ),
        ],
    )

    assert capsys.readouterr().out == (
        "HASHI\n"
        "Professional Agentic AI System\n"
        "Powered by HER-V2 - Flexible with CLI backends\n"
        "Designed by Barry Li\n"
        "\n"
        "Instance    HASHI3\n"
        "Location    C:\\Users\\thene\\projects\\HASHI3\n"
        "\n"
        "Agents\n"
        "  agent1    ONLINE | Telegram CONNECTED\n"
        "\n"
        "Services\n"
        "  Backend API  http://127.0.0.1:18804\n"
        "  API Gateway  http://127.0.0.1:18805\n"
        "\n"
    )


def test_static_header_uses_wsl_explorer_location(capsys) -> None:
    show_startup_header(
        instance_name="HASHI2",
        instance_path="/home/lily/projects/hashi2",
        platform_name="windows_wsl",
        distro_name="Ubuntu-22.04",
    )

    output = capsys.readouterr().out
    assert (
        "Location    "
        r"\\wsl.localhost\Ubuntu-22.04\home\lily\projects\hashi2"
    ) in output


def test_static_banner_removes_terminal_control_characters(capsys) -> None:
    show_startup_header(
        instance_name="HASHI3\x1b[31m",
        instance_path="safe\npath",
        platform_name="native_linux",
    )
    show_startup_status(
        agents=[AgentBannerStatus("agent\rname", "online\x1b", False)],
        services=[],
    )

    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "HASHI3?[31m" in output
    assert "Location    safe?path" in output
    assert "agent?name" in output


def test_safe_print_escapes_text_rejected_by_legacy_encoding(monkeypatch) -> None:
    class LegacyStream:
        encoding = "cp1252"

        def __init__(self) -> None:
            self.writes: list[str] = []

        def write(self, value: str) -> None:
            value.encode(self.encoding)
            self.writes.append(value)

        def flush(self) -> None:
            return None

    stream = LegacyStream()
    monkeypatch.setattr(sys, "stdout", stream)

    terminal_console.safe_print("测试")

    assert stream.writes == [r"\u6d4b\u8bd5" + "\n"]


def test_legacy_ascii_logo_uses_current_brand_without_fallback_warning(capsys) -> None:
    _show_ascii_startup_banner(agent_names=[], logo_only=True)

    output = capsys.readouterr().out
    assert "HASHI" in output
    assert "Professional Agentic AI System" in output
    assert "Powered by HER-V2 - Flexible with CLI backends" in output
    assert "Designed by Barry Li" in output
    assert "BRIDGE-U-F" not in output
    assert "terminal is not Unicode-safe" not in output


def test_startup_presentation_audit_is_structured_and_sanitised(tmp_path) -> None:
    terminal_console.configure(tmp_path)
    show_startup_header(
        instance_name="HASHI3",
        instance_path=tmp_path,
        platform_name="native_linux",
        audit_root=tmp_path,
    )
    show_startup_status(
        instance_name="HASHI3",
        agents=[AgentBannerStatus("agent1", "online", True)],
        services=[
            ServiceBannerStatus(
                "API Gateway",
                "http://user:PRIVATE-PASSWORD@127.0.0.1:18805?token=PRIVATE-TOKEN",
            )
        ],
        audit_root=tmp_path,
    )

    records = [
        json.loads(line)
        for line in (tmp_path / "logs" / "startup_presentation.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [record["section"] for record in records] == ["identity", "status"]
    assert records[0]["instance"] == "HASHI3"
    assert records[0]["delivery"]["delivered"] is True
    assert records[1]["agents"] == [
        {"name": "agent1", "state": "ONLINE", "telegram_connected": True}
    ]
    assert records[1]["services"] == [
        {"name": "API Gateway", "url": "http://127.0.0.1:18805"}
    ]
    encoded = json.dumps(records)
    assert "PRIVATE-PASSWORD" not in encoded
    assert "PRIVATE-TOKEN" not in encoded


def test_interactive_startup_animation_is_default_even_with_ascii_glyphs(
    monkeypatch,
    tmp_path,
) -> None:
    terminal_console.configure(tmp_path)
    rendered = []
    monkeypatch.setenv("BRIDGE_FORCE_ASCII_BANNER", "1")
    monkeypatch.setattr(
        banner,
        "prepare_terminal_animation",
        lambda: TerminalAnimationCapability(
            True,
            "interactive_ansi_terminal",
            "stdout",
        ),
    )
    monkeypatch.setattr(
        banner,
        "_show_animated_startup_banner",
        lambda **kwargs: rendered.append(kwargs),
    )

    result = show_startup_banner(
        ["agent1"],
        logo_only=True,
        audit_root=tmp_path,
        fallback_to_static=False,
    )

    assert result == StartupAnimationResult(
        completed=True,
        attempted=True,
        reason="interactive_ansi_terminal",
        sink="stdout",
    )
    assert len(rendered) == 1
    assert rendered[0]["agent_names"] == ["agent1"]
    assert rendered[0]["logo_only"] is True
    record = json.loads(
        (tmp_path / "logs" / "startup_presentation.jsonl").read_text(
            encoding="utf-8"
        )
    )
    assert record["section"] == "animation"
    assert record["outcome"] == "completed"


def test_animation_uses_static_fallback_only_for_technical_reason(
    monkeypatch,
) -> None:
    fallbacks = []
    monkeypatch.setattr(
        banner,
        "prepare_terminal_animation",
        lambda: TerminalAnimationCapability(False, "stdout_not_interactive"),
    )
    monkeypatch.setattr(
        banner,
        "_show_animated_startup_banner",
        lambda **_kwargs: pytest.fail("animation renderer should not run"),
    )
    monkeypatch.setattr(
        banner,
        "_show_ascii_startup_banner",
        lambda **kwargs: fallbacks.append(kwargs),
    )

    result = show_startup_banner(["agent1"], logo_only=True)

    assert result.completed is False
    assert result.attempted is False
    assert result.reason == "stdout_not_interactive"
    assert len(fallbacks) == 1


def test_animation_render_failure_is_diagnosed_without_payload(
    monkeypatch,
    tmp_path,
) -> None:
    terminal_console.configure(tmp_path)
    monkeypatch.setattr(
        banner,
        "prepare_terminal_animation",
        lambda: TerminalAnimationCapability(True, "interactive", "stdout"),
    )

    def fail_renderer(**_kwargs):
        raise OSError(5, "PRIVATE-ANIMATION-PAYLOAD")

    monkeypatch.setattr(banner, "_show_animated_startup_banner", fail_renderer)

    result = show_startup_banner(
        ["agent1"],
        audit_root=tmp_path,
        fallback_to_static=False,
    )

    assert result.completed is False
    assert result.attempted is True
    diagnostic = terminal_console.output_diagnostic_path().read_text(encoding="utf-8")
    assert '"purpose": "startup_animation"' in diagnostic
    assert "PRIVATE-ANIMATION-PAYLOAD" not in diagnostic
