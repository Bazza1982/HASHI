from __future__ import annotations

import hashlib
import json
import wave
from io import StringIO
from pathlib import Path

from rich.console import Console
from rich.text import Text
from textual.events import MouseMove, MouseScrollDown
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Static

from tui import sounds
from tui.app import (
    HASHITuiApp,
    ChatHistory,
    ChatInput,
    CommandPreview,
    FooterInfoBox,
    LogPanel,
    TypingIndicator,
    chat_message_renderable,
)
from tui.telegram_rendering import command_message_renderable
from tui.side_panel import SidePanel


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
        assert isinstance(app.focused, ChatInput)
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
        footer.update_state("Rika", "codex-cli", True, instance_id="HASHI1")
        assert "Instance HASHI1 · Agent Rika · Engine codex-cli" in footer.render().plain
        assert "API connected" in footer.render().plain
        assert "Flex" not in footer.render().plain

        app._load_initial_transcript = lambda *_args, **_kwargs: None
        app._select_agent(
            {
                "name": "akane",
                "display_name": "小茜",
                "active_backend": "codex-cli",
                "mode": "fixed",
            }
        )
        assert "Fixed" not in footer.render().plain


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
    assert preferences == {
        "theme": "retro",
        "language": "zh",
        "layout": "balanced",
        "sounds": True,
        "typing": True,
        "telegram_mirror": True,
        "sidepanel": False,
        "sidepanel_auto": False,
    }


async def test_tui_command_preview_discovers_dynamic_parameters_in_both_languages(tmp_path):
    app = HASHITuiApp(bridge_home=tmp_path, launch_instance_id="HASHI1")
    app._schedule_startup_sequence = lambda: None

    async with app.run_test(size=(132, 42)) as pilot:
        app._current_agent_metadata = {
            "allowed_backends": [
                {
                    "engine": "codex-cli",
                    "model": "gpt-6-astra",
                    "models": ["gpt-5.6-sol", "gpt-6-astra"],
                    "model_efforts": {
                        "gpt-6-astra": ["low", "medium", "high", "xhigh", "max"]
                    },
                },
                {"engine": "claude-cli", "model": "claude-sonnet-4-6"},
            ],
            "presentation_status": {
                "engine": "codex-cli",
                "model": "gpt-6-astra",
                "effort": "max",
            },
        }
        input_box = app.query_one("#chat-input", ChatInput)
        input_box.focus()

        app._handle_tui_cmd("/tui language zh")
        input_box.value = "/eff"
        await pilot.pause()
        preview = app.query_one("#command-preview", CommandPreview)
        rendered = _render_plain(preview.render())
        assert "/effort [level]" in rendered
        assert "可选" in rendered
        assert "low · medium · high · xhigh · max" in rendered
        assert "示例" in rendered
        assert "/effort high" in rendered

        input_box.value = "/effort "
        await pilot.pause()
        assert app._command_matches == [
            "/effort low",
            "/effort medium",
            "/effort high",
            "/effort xhigh",
            "/effort max",
        ]
        rendered = _render_plain(preview.render())
        assert "较少推理，响应更快" in rendered
        assert "当前选择" in rendered

        app._handle_tui_cmd("/tui language en")
        input_box.value = "/mode "
        await pilot.pause()
        assert app._command_matches == ["/mode fixed", "/mode flex"]
        rendered = _render_plain(preview.render())
        assert "Persistent engine session" in rendered
        assert "Options" in rendered
        assert "Example" in rendered

        input_box.value = "/sidepanel "
        await pilot.pause()
        assert app._command_matches == [
            "/sidepanel on",
            "/sidepanel off",
            "/sidepanel toggle",
            "/sidepanel refresh",
            "/sidepanel auto",
        ]
        rendered = _render_plain(preview.render())
        assert "Current selection" in rendered
        assert "/sidepanel on" in rendered

        input_box.value = "/sidepanel auto "
        await pilot.pause()
        assert app._command_matches == [
            "/sidepanel auto on",
            "/sidepanel auto off",
            "/sidepanel auto toggle",
        ]
        rendered = _render_plain(preview.render())
        assert "Options" in rendered
        assert "Enable automatic tour" in rendered


async def test_tui_no_argument_command_result_adds_local_parameter_guide(tmp_path):
    app = HASHITuiApp(bridge_home=tmp_path, launch_instance_id="HASHI1")
    app._schedule_startup_sequence = lambda: None

    async with app.run_test(size=(120, 40)):
        app._ui_language = "zh"
        app._current_agent_metadata = {
            "allowed_backends": [
                {
                    "engine": "codex-cli",
                    "model": "gpt-6-astra",
                    "model_efforts": {"gpt-6-astra": ["high", "max"]},
                }
            ],
            "presentation_status": {
                "engine": "codex-cli",
                "model": "gpt-6-astra",
                "effort": "max",
            },
        }
        app._render_command_result(
            {
                "ok": True,
                "command": "effort",
                "messages": [
                    {
                        "text": "<b>模型推理强度</b>\n当前 · max",
                        "meta": {"parse_mode": "HTML"},
                    }
                ],
            },
            agent="akane",
            submitted_text="/effort",
        )

        rendered = "\n".join(
            line.text for line in app.query_one("#chat-history", ChatHistory).lines
        )
        assert "可选 · high · max" in rendered
        assert "用法 · /effort [level]" in rendered
        assert "示例 · /effort high" in rendered


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

        input_box.value = "/mode fl"
        await pilot.pause()
        assert app._current_command_match == "/mode flex"
        await pilot.press("enter")
        assert sent == ["/mode", "/mode flex"]

        input_box.value = "/effortt"
        await pilot.press("enter")
        assert sent == ["/mode", "/mode flex"]
        chat = app.query_one("#chat-history", ChatHistory)
        rendered = "\n".join(line.text for line in chat.lines)
        assert "Unknown command: /effortt" in rendered

        # Exact non-menu commands remain valid even though they are not previewed.
        input_box.value = "/move"
        await pilot.press("enter")
        assert sent == ["/mode", "/mode flex", "/move"]


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


def test_command_message_safely_converts_telegram_html_and_keeps_literals():
    renderable = command_message_renderable(
        {
            "text": (
                "<b>Model</b> · &lt;safe&gt; [bold red]literal[/] "
                "<unsafe>x</unsafe> <a href=\"javascript:alert(1)\">link</a>\x1b"
            ),
            "meta": {"parse_mode": "HTML"},
        }
    )
    output = _render_plain(renderable)

    assert "Model · <safe>" in output
    assert "[bold red]literal[/]" in output
    assert "<unsafe>x</unsafe>" in output
    assert "\x1b" not in output
    assert all(span.style.link is None for span in renderable.spans)


async def test_command_response_messages_render_immediately_and_refresh_status(tmp_path):
    class CommandClient:
        proxied = False

        def __init__(self):
            self.sent = []

        async def send_chat(self, agent, text, **kwargs):
            self.sent.append((agent, text, kwargs))
            return {
                "ok": True,
                "slash_command": True,
                "command": "model",
                "messages": [
                    {
                        "channel": "reply",
                        "text": "<b>Model</b> · gpt-test",
                        "meta": {"parse_mode": "HTML"},
                    }
                ],
            }

        async def list_agents(self):
            return [
                {
                    "id": "akane",
                    "name": "akane",
                    "display_name": "Akane",
                    "active_backend": "codex-cli",
                    "online": True,
                    "presentation_status": {
                        "engine": "codex-cli",
                        "model": "gpt-test",
                        "effort": "high",
                        "think": True,
                        "verbose": False,
                        "commentary": True,
                    },
                }
            ]

        def reset_offset(self, _agent):
            return None

    app = HASHITuiApp(bridge_home=tmp_path, launch_instance_id="HASHI2")
    app._schedule_startup_sequence = lambda: None
    client = CommandClient()
    app.api = client

    async with app.run_test(size=(120, 40)) as pilot:
        app.gateway_ok = True
        app._load_initial_transcript = lambda *_args, **_kwargs: None
        app._select_agent((await client.list_agents())[0], client=client)
        app._submission_sequence = 1
        submission_ref = (0, "akane", 1)
        app._latest_submission_ref = submission_ref
        app._send_message(
            "/model",
            "akane",
            client,
            0,
            False,
            "en",
            submission_ref,
        )
        await pilot.pause(0.2)

        rendered = "\n".join(line.text for line in app.query_one("#chat-history", ChatHistory).lines)
        assert "Model · gpt-test" in rendered
        assert len(client.sent) == 1
        assert client.sent[0][2]["telegram_mirror"] is False
        assert "Effort high" in app.query_one("#footer-info-box", FooterInfoBox).render().plain


async def test_footer_shows_her_routes_but_never_invents_other_engine_provider(tmp_path):
    app = HASHITuiApp(bridge_home=tmp_path, launch_instance_id="HASHI2")
    app._schedule_startup_sequence = lambda: None

    async with app.run_test(size=(82, 30)):
        footer = app.query_one("#footer-info-box", FooterInfoBox)
        footer.update_state(
            "Akane",
            "her-v2",
            True,
            current_agent="akane",
            instance_id="HASHI2",
            metadata={
                "id": "akane",
                "display_name": "Akane",
                "presentation_status": {
                    "engine": "her-v2",
                    "effort": "planned",
                    "think": True,
                    "verbose": False,
                    "commentary": True,
                    "her_v2": {
                        "routing_mode": "hybrid",
                        "quick": {"provider": "openai", "model": "gpt-quick"},
                        "pro": {"provider": "anthropic", "model": "claude-pro"},
                    },
                },
            },
            telegram_mirror=False,
        )
        plain = footer.render().plain
        assert "Instance HASHI2" in plain
        assert "Agent Akane (akane)" in plain
        assert "Engine her-v2" in plain
        assert "Model Q:gpt-quick / P:claude-pro" in plain
        assert "Provider Q:openai / P:anthropic" in plain
        assert "Think ON · Verbose OFF · Commentary ON" in plain
        assert "TG mirror OFF" in plain
        assert "Flex" not in plain

        footer.update_state(
            "Rika",
            "codex-cli",
            True,
            current_agent="rika",
            instance_id="HASHI3",
            metadata={
                "id": "rika",
                "display_name": "Rika",
                "provider": "must-not-render",
                "presentation_status": {
                    "engine": "codex-cli",
                    "model": "gpt-codex",
                    "effort": "xhigh",
                    "think": False,
                    "verbose": True,
                    "commentary": False,
                },
            },
        )
        plain = footer.render().plain
        assert "Model gpt-codex" in plain
        assert "Provider" not in plain


async def test_footer_collapses_her_details_only_for_identical_providers(tmp_path):
    app = HASHITuiApp(bridge_home=tmp_path, launch_instance_id="HASHI1")
    app._schedule_startup_sequence = lambda: None

    async with app.run_test(size=(120, 30)):
        footer = app.query_one("#footer-info-box", FooterInfoBox)
        footer.update_state(
            "临时员工",
            "her-v2",
            True,
            current_agent="temp",
            instance_id="HASHI1",
            language="zh",
            metadata={
                "id": "temp",
                "display_name": "临时员工",
                "presentation_status": {
                    "engine": "her-v2",
                    "effort": "zero",
                    "her_v2": {
                        "routing_mode": "single",
                        "quick": {
                            "provider": "openrouter-api",
                            "model": "deepseek/deepseek-v3.2-exp",
                        },
                        "pro": {
                            "provider": "openrouter-api",
                            "model": "deepseek/deepseek-v3.2-exp",
                        },
                    },
                },
            },
        )
        plain = footer.render().plain
        assert "模型 deepseek/deepseek-v3.2-exp" in plain
        assert "模型 Q:" not in plain
        assert "模型提供商 openrouter-api" in plain
        assert "模型提供商 Q:" not in plain

        footer.update_state(
            "Temp",
            "her-v2",
            True,
            current_agent="temp",
            instance_id="HASHI1",
            language="en",
            metadata={
                "id": "temp",
                "display_name": "Temp",
                "presentation_status": {
                    "engine": "her-v2",
                    "effort": "medium",
                    "her_v2": {
                        "routing_mode": "hybrid",
                        "quick": {
                            "provider": "openrouter-api",
                            "model": "deepseek/quick",
                        },
                        "pro": {
                            "provider": "openrouter-api",
                            "model": "deepseek/pro",
                        },
                    },
                },
            },
        )
        plain = footer.render().plain
        assert "Model Q:deepseek/quick / P:deepseek/pro" in plain
        assert "Provider openrouter-api" in plain
        assert "Provider Q:" not in plain


async def test_chat_selection_pauses_follow_tail_copies_and_escape_resumes(
    tmp_path,
    monkeypatch,
):
    copied = []
    monkeypatch.setattr("tui.app.copy_to_windows_clipboard", lambda text: copied.append(text) or True)
    app = HASHITuiApp(bridge_home=tmp_path, launch_instance_id="HASHI2")
    app._schedule_startup_sequence = lambda: None

    async with app.run_test(size=(80, 24)) as pilot:
        chat = app.query_one("#chat-history", ChatHistory)
        chat.write(Text("copy me"))
        await pilot.pause()
        selection = Selection(Offset(0, 0), Offset(7, 0))
        app.screen.selections = {chat: selection}
        await pilot.pause()
        assert chat.auto_scroll is False
        assert app.screen.get_selected_text() == "copy me"
        selected_line = chat._render_line(0, 0, 10)
        selection_style = app.screen.get_component_rich_style("screen--selection")
        assert any(
            segment.style is not None
            and segment.style.bgcolor == selection_style.bgcolor
            for segment in selected_line
        )

        await pilot.press("ctrl+c")
        assert copied == ["copy me"]

        await pilot.press("escape")
        assert app.screen.get_selected_text() is None
        assert chat.auto_scroll is True


async def test_mouse_drag_creates_chat_selection(tmp_path):
    app = HASHITuiApp(bridge_home=tmp_path, launch_instance_id="HASHI2")
    app._schedule_startup_sequence = lambda: None

    async with app.run_test(size=(80, 24)) as pilot:
        chat = app.query_one("#chat-history", ChatHistory)
        chat.write(Text("mouse copy"))
        await pilot.pause()

        await pilot.mouse_down(chat, offset=(1, 1))
        await pilot._post_mouse_events(
            [MouseMove],
            chat,
            offset=(6, 1),
            button=1,
        )
        await pilot.mouse_up(chat, offset=(6, 1))

        assert app.screen.get_selected_text() == "mouse"
        assert chat.auto_scroll is False


async def test_typing_and_telegram_preferences_are_persistent_and_run_fenced(tmp_path):
    app = HASHITuiApp(bridge_home=tmp_path, launch_instance_id="HASHI2")
    app._schedule_startup_sequence = lambda: None

    async with app.run_test(size=(100, 30)):
        app.current_agent = "akane"
        app.current_agent_display = "Akane"
        first = (0, "akane", "session-1", "run-1", "request-1")
        second = (0, "akane", "session-1", "run-2", "request-2")
        indicator = app.query_one("#typing-indicator", TypingIndicator)

        app._set_typing_run(first, phase="queued")
        assert indicator.display is True
        assert "Queued" in indicator.render().plain
        app._set_typing_run(second, phase="running")
        assert "Akane is typing" in indicator.render().plain
        app._clear_typing_indicator(first)
        assert app._active_run_ref == second
        assert indicator.display is True

        app._handle_tui_cmd("/tui typing off")
        assert indicator.display is False
        app._handle_tui_cmd("/tui typing on")
        assert indicator.display is True
        app._handle_telegram_cmd("/telegram off")
        assert app._telegram_mirror_enabled is False
        app._clear_typing_indicator(second)
        assert indicator.display is False

    preferences = json.loads((tmp_path / "state" / "tui_preferences.json").read_text())
    assert preferences["typing"] is True
    assert preferences["telegram_mirror"] is False
    reloaded = HASHITuiApp(bridge_home=tmp_path, launch_instance_id="HASHI2")
    assert reloaded._tui_typing_enabled is True
    assert reloaded._telegram_mirror_enabled is False


async def test_sidepanel_is_read_only_bilingual_and_persistent(tmp_path):
    class SidePanelClient:
        proxied = False

        def __init__(self):
            self.sent = []

        async def agent_overview(self, agent):
            assert agent == "akane"
            return {
                "ok": True,
                "overview": {
                    "usage": {
                        "session": {
                            "requests": 2,
                            "input": 120,
                            "output": 80,
                            "thinking": 30,
                            "total": 230,
                            "cost_usd": 0.12,
                            "unknown_cost_requests": 1,
                        },
                        "all_time": {
                            "requests": 12,
                            "input": 2_500,
                            "output": 1_400,
                            "thinking": 300,
                            "total": 4_200,
                            "cost_usd": 1.5,
                            "unknown_cost_requests": 0,
                        },
                    },
                    "system_prompts": {
                        "active_count": 2,
                        "configured_count": 3,
                        "total_count": 4,
                        "slots": [
                            {
                                "slot": "persona",
                                "state": "on",
                                "characters": 321,
                                "preview": "Friendly assistant",
                            }
                        ],
                    },
                    "parked_topics": {
                        "count": 1,
                        "topics": [
                            {
                                "slot": 2,
                                "title": "TUI polish",
                                "followup": {"status": "parked"},
                            }
                        ],
                    },
                },
            }

        async def scheduler_jobs(self, agent):
            assert agent == "akane"
            return {
                "ok": True,
                "jobs": [
                    {
                        "id": "nightly-mail",
                        "kind": "cron",
                        "enabled": True,
                        "last_status": "completed",
                    }
                ],
            }

        async def background_jobs(self, agent, limit=20):
            assert (agent, limit) == ("akane", 20)
            return {
                "ok": True,
                "jobs": [{"job_id": "job-7", "status": "running"}],
            }

        async def send_chat(self, agent, text, **kwargs):
            self.sent.append((agent, text, kwargs))
            return {"ok": True}

    app = HASHITuiApp(bridge_home=tmp_path, launch_instance_id="HASHI1")
    app._schedule_startup_sequence = lambda: None
    client = SidePanelClient()
    app.api = client

    async with app.run_test(size=(120, 40)) as pilot:
        app.gateway_ok = True
        app.current_agent = "akane"
        app.current_agent_display = "Akane"
        app.current_backend = "her-v2"
        app._agents_cache = [
            {
                "name": "akane",
                "display_name": "Akane",
                "active_backend": "her-v2",
                "online": True,
                "queue_depth": 1,
            },
            {
                "name": "rika",
                "display_name": "Rika",
                "active_backend": "codex-cli",
                "online": False,
                "queue_depth": 0,
            },
        ]
        app._ui_language = "zh"
        input_box = app.query_one("#chat-input", ChatInput)
        input_box.focus()
        input_box.value = "/sidepanel"
        await pilot.press("enter")
        await pilot.pause()

        panel = app.query_one("#side-panel", SidePanel)
        assert panel.display is True
        plain = panel.content_text.plain
        assert panel.border_title == "信息面板"
        assert plain.splitlines()[0] == "Token 用量"
        assert "Token 用量" in plain
        assert "本次会话" in plain and "230" in plain
        assert "任务" in plain and "nightly-mail" in plain and "job-7" in plain
        assert "HASHI 上下文" in plain and "Friendly assistant" in plain
        assert "暂存主题" in plain and "TUI polish" in plain
        assert "代理" in plain and "Akane" in plain and "Rika" in plain
        assert "只读" not in plain
        assert "HASHI1" not in plain
        assert client.sent == []

        app._handle_tui_cmd("/tui language en")
        plain = panel.content_text.plain
        assert panel.border_title == "Information"
        assert plain.splitlines()[0] == "Token usage"
        assert "Token usage" in plain
        assert "Jobs" in plain
        assert "HASHI context" in plain
        assert "Parked topics" in plain
        assert "Agents" in plain
        assert "Read only" not in plain

        input_box.value = "/sidepanel off"
        await pilot.press("enter")
        await pilot.pause()
        assert panel.display is False
        assert client.sent == []

    preferences = json.loads((tmp_path / "state" / "tui_preferences.json").read_text())
    assert preferences["sidepanel"] is False
    assert preferences["sidepanel_auto"] is False
    reloaded = HASHITuiApp(bridge_home=tmp_path, launch_instance_id="HASHI1")
    assert reloaded._side_panel_enabled is False
    assert reloaded._side_panel_auto_scroll is False


async def test_sidepanel_keeps_chat_visible_in_narrow_terminal(tmp_path):
    app = HASHITuiApp(bridge_home=tmp_path, launch_instance_id="HASHI1")
    app._schedule_startup_sequence = lambda: None

    async with app.run_test(size=(72, 28)) as pilot:
        app._side_panel_enabled = True
        app._apply_side_panel_visibility(persist=False)
        await pilot.pause()

        panel = app.query_one("#side-panel")
        chat = app.query_one("#chat-container")
        assert panel.display is True
        assert panel.region.width <= 32
        assert chat.region.width >= 30


async def test_sidepanel_is_actually_scrollable_by_pointer_and_keyboard(tmp_path):
    app = HASHITuiApp(bridge_home=tmp_path, launch_instance_id="HASHI1")
    app._schedule_startup_sequence = lambda: None

    overview = {
        "usage": {
            "session": {"requests": 1, "total": 10},
            "all_time": {"requests": 2, "total": 20},
        },
        "system_prompts": {"slots": []},
        "parked_topics": {"topics": []},
    }
    agents = [
        {
            "name": f"agent-{index}",
            "display_name": f"Agent {index}",
            "online": True,
        }
        for index in range(18)
    ]

    async with app.run_test(size=(72, 20)) as pilot:
        app._side_panel_enabled = True
        app._apply_side_panel_visibility(persist=False)
        panel = app.query_one("#side-panel", SidePanel)
        panel.update_dashboard(
            instance_id="HASHI1",
            current_agent="agent-0",
            current_agent_display="Agent 0",
            current_backend="her-v2",
            gateway_ok=True,
            overview=overview,
            scheduler_jobs=[],
            background_jobs=[],
            agents=agents,
            language="zh",
        )
        await pilot.pause()

        assert panel.max_scroll_y > 0
        assert panel.show_vertical_scrollbar is True
        await pilot.click(panel, offset=(2, 2))
        assert app.focused is panel

        await pilot.press("down")
        await pilot.pause()
        assert panel.scroll_y > 0

        before_page = panel.scroll_y
        await pilot.press("pagedown")
        await pilot.pause()
        assert panel.scroll_y > before_page

        await pilot.press("end")
        await pilot.wait_for_scheduled_animations()
        assert panel.scroll_y == panel.max_scroll_y
        await pilot.press("home")
        await pilot.wait_for_scheduled_animations()
        assert panel.scroll_y == 0

        content = panel.query_one("#side-panel-content", Static)
        content.post_message(
            MouseScrollDown(
                content,
                x=2,
                y=2,
                delta_x=0,
                delta_y=1,
                button=0,
                shift=False,
                meta=False,
                ctrl=False,
                screen_x=content.region.x + 2,
                screen_y=content.region.y + 2,
            )
        )
        await pilot.pause()
        assert panel.scroll_y > 0

        panel.update_dashboard(
            instance_id="HASHI1",
            current_agent="agent-0",
            current_agent_display="Agent 0",
            current_backend="her-v2",
            gateway_ok=True,
            overview=overview,
            scheduler_jobs=[],
            background_jobs=[],
            agents=agents,
            language="en",
        )
        await pilot.pause()
        assert panel.content_text.plain.splitlines()[0] == "Token usage"
        assert panel.max_scroll_y > 0
        await pilot.press("end")
        await pilot.wait_for_scheduled_animations()
        assert panel.scroll_y == panel.max_scroll_y
        await pilot.press("home")
        await pilot.wait_for_scheduled_animations()
        assert panel.scroll_y == 0


async def test_sidepanel_auto_tour_is_bilingual_looping_and_persistent(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(SidePanel, "AUTO_SCROLL_INTERVAL_S", 3_600.0)
    app = HASHITuiApp(bridge_home=tmp_path, launch_instance_id="HASHI1")
    app._schedule_startup_sequence = lambda: None

    async with app.run_test(size=(72, 20)) as pilot:
        await app._handle_sidepanel_cmd("/sidepanel auto on")
        panel = app.query_one("#side-panel", SidePanel)
        panel.update_dashboard(
            instance_id="HASHI1",
            current_agent="agent-0",
            current_agent_display="Agent 0",
            current_backend="her-v2",
            gateway_ok=True,
            overview={
                "usage": {
                    "session": {"requests": 1, "total": 10},
                    "all_time": {"requests": 2, "total": 20},
                },
                "system_prompts": {"slots": []},
                "parked_topics": {"topics": []},
            },
            scheduler_jobs=[],
            background_jobs=[],
            agents=[
                {
                    "name": f"agent-{index}",
                    "display_name": f"Agent {index}",
                    "online": True,
                }
                for index in range(18)
            ],
            language="zh",
        )
        await pilot.pause()

        assert app._side_panel_enabled is True
        assert app._side_panel_auto_scroll is True
        assert panel.auto_scroll_enabled is True
        assert panel.border_title == "信息面板 · 自动巡览"
        panel.scroll_home(animate=False)
        panel.advance_auto_scroll()
        await pilot.pause()
        assert panel.scroll_y > 0

        panel.scroll_end(animate=False)
        await pilot.pause()
        for _ in range(panel.AUTO_SCROLL_BOTTOM_HOLD_TICKS):
            panel.advance_auto_scroll()
        await pilot.pause()
        assert panel.scroll_y == 0

        app._handle_tui_cmd("/tui language en")
        assert panel.border_title == "Information · Auto tour"
        await app._handle_sidepanel_cmd("/sidepanel auto off")
        assert app._side_panel_enabled is True
        assert app._side_panel_auto_scroll is False
        assert panel.auto_scroll_enabled is False
        assert panel.border_title == "Information"

    preferences = json.loads((tmp_path / "state" / "tui_preferences.json").read_text())
    assert preferences["sidepanel"] is True
    assert preferences["sidepanel_auto"] is False
    reloaded = HASHITuiApp(bridge_home=tmp_path, launch_instance_id="HASHI1")
    assert reloaded._side_panel_enabled is True
    assert reloaded._side_panel_auto_scroll is False


async def test_theme_switch_recolors_history_preserves_draft_and_preferences(tmp_path):
    from tui.themes import PALETTES
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "tui_preferences.json").write_text('{"future_option": 42}')
    app = HASHITuiApp(bridge_home=tmp_path, launch_instance_id="HASHI1")
    app._schedule_startup_sequence = lambda: None
    async with app.run_test(size=(80, 30)) as pilot:
        chat = app.query_one(ChatHistory)
        draft = app.query_one(ChatInput)
        draft.value = "unfinished 中文 draft"
        chat.write(chat_message_renderable("assistant", "Agent", "Existing **message**\n```python\nprint(42)\n```"))
        await pilot.pause()
        original = chat.lines[0]
        for name in PALETTES:
            app._handle_theme_cmd("/theme " + name)
            await pilot.pause()
            assert app.theme == "hashi-" + name
            assert "Existing" in "\n".join(line.text for line in chat.lines)
            assert draft.value == "unfinished 中文 draft"
            if name == "win32":
                assert list(chat.lines[0]) != list(original)
                assert app.query_one(ChatHistory).styles.background.hex == "#EEEEEE"
        app._handle_theme_cmd("/theme nonsense")
        assert app.theme == "hashi-atm"
        saved = json.loads(app._preferences_path.read_text())
        assert saved["theme"] == "atm" and saved["future_option"] == 42
    reopened = HASHITuiApp(bridge_home=tmp_path, launch_instance_id="HASHI1")
    assert reopened.theme == "hashi-atm"


async def test_fresh_tui_keeps_accepted_chat_when_optional_status_api_is_disabled(tmp_path):
    class Client:
        checks = 0
        async def send_chat(self, *args, **kwargs):
            return {'ok':True,'session_id':'session','run_id':'run','request_id':'request'}
        async def run_info(self, *args):
            self.checks += 1
            return {'ok':False,'code':'session_api_not_ready','error':'Optional status API disabled'}
    app=HASHITuiApp(bridge_home=tmp_path,launch_instance_id='TEST')
    app._schedule_startup_sequence=lambda:None
    client=Client();app.api=client
    async with app.run_test() as pilot:
        app.gateway_ok=True;app.current_agent='hashiko'
        field=app.query_one(ChatInput);field.value='hello'
        await pilot.press('enter');await pilot.pause(1.2)
        assert client.checks==1
        assert 'Run status unavailable' not in '\n'.join(line.text for line in app.query_one(ChatHistory).lines)
        assert app._active_run_ref is None
