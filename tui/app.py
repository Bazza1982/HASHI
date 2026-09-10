"""HASHI TUI — Textual-based terminal UI wrapping main.py."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from uuid import uuid4

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.strip import Strip
from textual.suggester import SuggestFromList
from textual.widgets import Input, RichLog, Static

from orchestrator import ui_language
from orchestrator.command_specs import COMMAND_SPECS, CommandGuide
from orchestrator.flexible_backend_registry import (
    HER_V2_ENGINE,
    canonical_backend_engine,
    get_available_models,
    is_selectable_backend,
)
from orchestrator.runtime_effort_options import get_available_efforts
from orchestrator.runtime_defaults import DEFAULT_WORKBENCH_LOCALHOST_URL
from tui.api_client import TUI_TERMINAL_RUN_STATES, TuiApiClient, run_failure_text
from tui.clipboard import copy_to_windows_clipboard
from tui.instances import InstanceResolver, InstanceTarget, load_launch_instance
from tui.light_onboarding import LightOnboardingPhase, is_onboarding_complete
from tui.onboarding import (
    audit_environment,
    lang_code_from_file,
    load_languages,
    verify_openrouter,
    write_config,
)
from tui.sounds import play_message_sound
from tui.side_panel import SidePanel
from tui.telegram_rendering import command_message_renderable

logger = logging.getLogger(__name__)

STARTUP_LOGO = (
    "  ██╗  ██╗  █████╗ ███████╗██╗  ██╗██╗",
    "  ██║  ██║ ██╔══██╗██╔════╝██║  ██║██║",
    "  ███████║ ███████║███████╗███████║██║",
    "  ██╔══██║ ██╔══██║╚════██║██╔══██║██║",
    "  ██║  ██║ ██║  ██║███████║██║  ██║██║",
    "  ╚═╝  ╚═╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝",
)

TUI_COMMAND_HELP = {
    "help": ("查看 TUI 与 Agent 命令", "Show TUI and Agent commands"),
    "to": ("切换聊天目标", "Change the chat target"),
    "instance": ("查看或切换 HASHI 实例", "List or switch HASHI instances"),
    "layout": ("调整日志与聊天布局", "Resize log and chat panes"),
    "log": ("显示、隐藏或暂停主机日志", "Show, hide, or pause the host log"),
    "agents": ("查看可用 Agent", "List available Agents"),
    "clear": ("清空当前 TUI 显示", "Clear the current TUI view"),
    "quit": ("退出 TUI", "Exit the TUI"),
    "tui": ("设置 TUI 语言及客户端选项", "Set TUI language and client options"),
}
TUI_COMMAND_HELP["telegram"] = (
    "\u67e5\u770b\u6216\u8bbe\u7f6e TUI Telegram \u955c\u50cf",
    "Inspect or set TUI Telegram mirroring",
)
TUI_COMMAND_HELP["sidepanel"] = (
    "控制只读信息面板及自动巡览",
    "Control the read-only information panel and automatic tour",
)
TUI_COMMAND_GUIDES = {
    "help": CommandGuide("/help [zh|en]", ("zh", "en"), example="/help zh"),
    "to": CommandGuide("/to <agent|all>", choice_source="agents"),
    "instance": CommandGuide(
        "/instance [name|refresh]", ("refresh",), example="/instance refresh"
    ),
    "layout": CommandGuide(
        "/layout [chat|balanced|compact|reset]",
        ("chat", "balanced", "compact", "reset"),
        example="/layout balanced",
    ),
    "log": CommandGuide(
        "/log [show|hide|pause]",
        ("show", "hide", "pause"),
        example="/log hide",
    ),
    "tui": CommandGuide(
        "/tui <language|sound|typing|telegram> <value>",
        ("language", "sound", "typing", "telegram"),
        example="/tui language zh",
    ),
    "telegram": CommandGuide(
        "/telegram [on|off]", ("on", "off"), example="/telegram off"
    ),
    "sidepanel": CommandGuide(
        "/sidepanel [on|off|toggle|refresh|auto <on|off|toggle>]",
        ("on", "off", "toggle", "refresh", "auto"),
        example="/sidepanel on",
    ),
}
TUI_NESTED_CHOICES = {
    ("tui", "language"): ("zh", "en"),
    ("tui", "sound"): ("on", "off", "test"),
    ("tui", "typing"): ("on", "off"),
    ("tui", "telegram"): ("on", "off"),
    ("sidepanel", "auto"): ("on", "off", "toggle"),
}
TUI_LOCALIZED_EXAMPLES = {
    "handoff": ("继续当前任务", "continue the current task"),
    "ticket": ("TUI 命令菜单不清楚", "TUI command menu is unclear"),
    "park": ("chat 稍后继续", "chat follow up later"),
    "new": ("研究笔记", "research notes"),
    "nudge": ("15 直到任务完成", "15 until the task is complete"),
    "debug": ("诊断这个故障", "diagnose this failure"),
    "exp": ("审查这个结果", "review this result"),
    "steer": ("保留当前文件", "keep the current files"),
    "focus": ("只检查测试", "tests only"),
    "wa_send": ("+61400000000 您好", "+61400000000 Hello"),
    "browser": ("3 搜索今天的天气", "3 search today's weather"),
    "long": ("审查这些文件", "review these files"),
}
TUI_DISCOVERY_COMMANDS = ("/help", "/to", "/mode", "/model", "/backend")


def markup(text: str) -> Text:
    """Render Rich markup explicitly before writing into RichLog."""
    return Text.from_markup(text)


def _enabled_setting(value, *, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() not in {"0", "false", "no", "off"}


def chat_message_renderable(role: str, prefix: str, body: str) -> Group:
    """Build one safe chat entry, rendering assistant content as Markdown."""

    header = Text()
    if str(role).strip().casefold() == "assistant":
        header.append(f"{prefix}:", style="bold #63ffd9")
        content = Markdown(
            str(body),
            code_theme="monokai",
            hyperlinks=True,
        )
    else:
        header.append(f"{prefix}:", style="bold #71b7ff")
        # User input is literal text.  In particular, square brackets must not
        # be interpreted as Rich markup tags.
        content = Text(str(body))
    return Group(header, content)


# ── Widgets ─────────────────────────────────────────────────────────────────

class LogPanel(RichLog):
    """Upper panel — streams stdout from the bridge subprocess."""
    DEFAULT_CSS = """
    LogPanel {
        height: 1fr;
        background: #08131d;
        color: #dff6ff;
        border: solid #2a5b82;
        border-title-align: left;
        scrollbar-background: #050b12;
        scrollbar-color: #2a5b82;
        scrollbar-color-hover: #71b7ff;
        scrollbar-color-active: #63ffd9;
    }
    """

    def on_mount(self):
        self.border_title = "HASHI Log"
        self.wrap = True


class ChatHistory(RichLog):
    """Chat display area showing agent replies."""
    DEFAULT_CSS = """
    ChatHistory {
        height: 1fr;
        background: #091722;
        color: #dff6ff;
        border: solid #2a5b82;
        border-title-align: left;
        min-height: 6;
        scrollbar-background: #050b12;
        scrollbar-color: #2a5b82;
        scrollbar-color-hover: #71b7ff;
        scrollbar-color-active: #63ffd9;
    }
    """

    def on_mount(self):
        self.border_title = "Chat"
        self.wrap = True

    def selection_updated(self, selection):
        """Freeze follow-tail while the user is dragging a text selection."""

        super().selection_updated(selection)
        self.auto_scroll = selection is None

    def _render_line(self, y: int, scroll_x: int, width: int) -> Strip:
        """Render Rich content with Textual's screen selection style."""

        # RichLog omits the per-cell selection offsets that Textual's newer
        # Log widget supplies. Add them so mouse drags resolve to character
        # positions rather than selecting the entire widget as one block.
        line = super()._render_line(y, scroll_x, width).apply_offsets(scroll_x, y)
        selection = self.text_selection
        if selection is None or (span := selection.get_span(y)) is None:
            return line
        start, end = span
        visible_start = max(0, start - scroll_x)
        visible_end = width if end == -1 else min(width, end - scroll_x)
        if visible_end <= visible_start:
            return line
        selection_style = self.screen.get_component_rich_style("screen--selection")
        return Strip.join(
            (
                line.crop(0, visible_start),
                line.crop(visible_start, visible_end).apply_style(selection_style),
                line.crop(visible_end, width),
            )
        )

    def get_selection(self, selection):
        """Extract selected text from RichLog's rendered line buffer.

        Textual's generic widget implementation asks ``render()`` for a text
        visual. ``RichLog`` renders through ``render_line()`` instead, so its
        inherited implementation sees a diagnostic panel and returns no text.
        Keep the visual cell widths while extracting, then remove padding that
        RichLog adds to the end of each rendered row.
        """

        rendered = "\n".join(line.text for line in self.lines)
        selected = selection.extract(rendered)
        return "\n".join(row.rstrip() for row in selected.splitlines()), "\n"

    def resume_auto_scroll(self) -> None:
        self.auto_scroll = True
        if self.is_mounted:
            self.scroll_end(animate=False, immediate=False, x_axis=False)


class ChatInput(Input):
    """Single-line input for sending messages."""
    DEFAULT_CSS = """
    ChatInput {
        height: 3;
        background: #0b1824;
        color: #dff6ff;
        border: solid #2a5b82;
    }
    ChatInput:focus {
        border: solid #63ffd9;
    }
    """


class CommandPreview(Static):
    """Compact slash-command palette shown below the input."""

    DEFAULT_CSS = """
    CommandPreview {
        display: none;
        height: auto;
        max-height: 10;
        padding: 0 1;
        background: #101d28;
        color: #9be7ff;
    }
    """

    def show_matches(
        self,
        matches: list[tuple[str, str, str]],
        selected_index: int = 0,
        details: tuple[str, ...] = (),
    ):
        rows = Text()
        for index, (command, description, scope) in enumerate(matches):
            selected = index == selected_index
            rows.append("› " if selected else "  ", style="bold #63ffd9" if selected else "#39566e")
            rows.append(command, style="bold #71b7ff" if selected else "#7dc6ff")
            rows.append(" " * max(2, 34 - len(command)))
            rows.append(description, style="#dff6ff" if selected else "#9fb3c8")
            rows.append(f"  {scope}", style="dim #7fb6c7")
            if index < len(matches) - 1:
                rows.append("\n")
        for detail in details:
            if rows:
                rows.append("\n")
            rows.append(f"  {detail}", style="dim #9be7ff")
        self.styles.height = "auto"
        self.update(rows)
        self.display = True

    def hide_match(self):
        self.display = False


class TypingIndicator(Static):
    """Ephemeral, Run-bound TUI queue/typing projection."""

    DEFAULT_CSS = """
    TypingIndicator {
        display: none;
        height: 1;
        padding: 0 1;
        background: #091722;
        color: #9be7ff;
    }
    """

    _FRAMES = ("\u2026", "\u00b7", "\u00b7\u00b7", "\u00b7\u00b7\u00b7")

    def __init__(self, *args, **kwargs):
        super().__init__("", *args, **kwargs)
        self._agent = ""
        self._language = "en"
        self._phase = "idle"
        self._frame = 0

    def on_mount(self):
        self.set_interval(0.4, self._advance)

    def show_run(self, *, agent: str, phase: str, language: str) -> None:
        self._agent = str(agent or "Agent")
        self._language = "zh" if language == "zh" else "en"
        self._phase = "running" if phase == "running" else "queued"
        self._frame = 0
        self.display = True
        self._render_state()

    def clear_run(self) -> None:
        self._phase = "idle"
        self._agent = ""
        self.display = False
        self.update(Text(""))

    def refresh_language(self, language: str) -> None:
        self._language = "zh" if language == "zh" else "en"
        if self._phase != "idle":
            self._render_state()

    def _advance(self) -> None:
        if self._phase != "running" or not self.display:
            return
        self._frame = (self._frame + 1) % len(self._FRAMES)
        self._render_state()

    def _render_state(self) -> None:
        if self._phase == "queued":
            value = (
                f"\u5df2\u6392\u961f\uff0c\u7b49\u5f85 {self._agent}\u2026"
                if self._language == "zh"
                else f"Queued \u00b7 waiting for {self._agent}\u2026"
            )
        else:
            suffix = self._FRAMES[self._frame]
            value = (
                f"{self._agent} \u6b63\u5728\u8f93\u5165{suffix}"
                if self._language == "zh"
                else f"{self._agent} is typing{suffix}"
            )
        self.update(Text(value, style="italic #9be7ff"))


class FooterInfoBox(Static):
    """Compact footer backed only by authoritative runtime/API facts."""

    DEFAULT_CSS = """
    FooterInfoBox {
        height: auto;
        min-height: 4;
        background: #050b12;
        color: #dff6ff;
        border: solid #2a5b82;
        padding: 0 1;
        margin-top: 0;
    }
    """

    def __init__(self, *args, **kwargs):
        super().__init__("", *args, **kwargs)
        self._content = Text("")
        self._status_line = "❌ HASHI · No agent · API offline"
        self._language = "en"

    def on_mount(self):
        self._refresh_footer()

    def update_state(
        self,
        agent: str = "",
        backend: str = "",
        gateway_ok: bool = False,
        agents: list[dict] | None = None,
        current_agent: str | None = None,
        instance_id: str = "",
        language: str = "en",
        metadata: dict | None = None,
        telegram_mirror: bool = True,
    ):
        self._language = language
        facts = dict(metadata or {})
        presentation = facts.get("presentation_status")
        presentation = dict(presentation) if isinstance(presentation, dict) else {}
        labels = (
            {
                "instance": "实例",
                "agent": "Agent",
                "engine": "Engine",
                "model": "模型",
                "provider": "模型提供商",
                "route": "路由",
                "effort": "推理强度",
                "think": "Think",
                "verbose": "Verbose",
                "commentary": "Commentary",
                "mirror": "TG 镜像",
            }
            if language == "zh"
            else {
                "instance": "Instance",
                "agent": "Agent",
                "engine": "Engine",
                "model": "Model",
                "provider": "Provider",
                "route": "Route",
                "effort": "Effort",
                "think": "Think",
                "verbose": "Verbose",
                "commentary": "Commentary",
                "mirror": "TG mirror",
            }
        )
        display_name = str(facts.get("display_name") or agent or "").strip()
        agent_id = str(facts.get("id") or facts.get("name") or current_agent or "").strip()
        engine = str(
            presentation.get("engine")
            or facts.get("active_backend")
            or facts.get("engine")
            or backend
            or ""
        ).strip()
        no_agent = "未选择 Agent" if language == "zh" else "No agent"
        agent_label = display_name or agent_id or no_agent
        if agent_id and display_name and agent_id != display_name:
            agent_label = f"{display_name} ({agent_id})"
        first = [
            "✅" if gateway_ok else "❌",
            f"{labels['instance']} {instance_id or 'HASHI'}",
            f"{labels['agent']} {agent_label}",
        ]
        if engine:
            first.append(f"{labels['engine']} {engine}")

        second: list[str] = []
        her = presentation.get("her_v2")
        if engine == "her-v2" and isinstance(her, dict):
            quick = her.get("quick") if isinstance(her.get("quick"), dict) else {}
            pro = her.get("pro") if isinstance(her.get("pro"), dict) else {}
            quick_model = str(quick.get("model") or "").strip()
            pro_model = str(pro.get("model") or "").strip()
            quick_provider = str(quick.get("provider") or "").strip()
            pro_provider = str(pro.get("provider") or "").strip()
            identical_provider = bool(
                quick_provider
                and pro_provider
                and quick_provider == pro_provider
            )
            identical_model = bool(
                identical_provider
                and quick_model
                and pro_model
                and quick_model == pro_model
            )
            if identical_model:
                second.append(f"{labels['model']} {quick_model}")
            else:
                second.append(
                    f"{labels['model']} Q:{quick_model or '?'} / P:{pro_model or '?'}"
                )
            if identical_provider:
                second.append(f"{labels['provider']} {quick_provider}")
            else:
                second.append(
                    f"{labels['provider']} Q:{quick_provider or '?'} / P:{pro_provider or '?'}"
                )
            if her.get("routing_mode"):
                second.append(f"{labels['route']} {her['routing_mode']}")
        else:
            model = str(presentation.get("model") or facts.get("model") or "").strip()
            if model:
                second.append(f"{labels['model']} {model}")

        def switch(name: str) -> str:
            value = presentation.get(name)
            return "ON" if value is True else "OFF" if value is False else "n/a"

        effort = str(presentation.get("effort") or "n/a")
        second.extend(
            [
                f"{labels['effort']} {effort}",
                f"{labels['think']} {switch('think')}",
                f"{labels['verbose']} {switch('verbose')}",
                f"{labels['commentary']} {switch('commentary')}",
                "API 已连接" if language == "zh" and gateway_ok else
                "API 离线" if language == "zh" else
                "API connected" if gateway_ok else "API offline",
                f"{labels['mirror']} {'ON' if telegram_mirror else 'OFF'}",
            ]
        )
        self._status_line = " · ".join(first) + "\n" + " · ".join(second)
        self._refresh_footer()

    def _refresh_footer(self):
        self._content = Text(self._status_line, style="bold #63ffd9")
        self.refresh()

    def render(self) -> Text:
        return self._content


# ── Onboarding Screen (inline in main app) ──────────────────────────────────

class OnboardingPhase:
    """State machine for the onboarding flow running inside the TUI."""

    PHASE_LANG = "lang"
    PHASE_DISCLAIMER = "disclaimer"
    PHASE_AUDIT = "audit"
    PHASE_OPENROUTER = "openrouter"
    PHASE_DONE = "done"

    def __init__(self, bridge_home: Path):
        self.bridge_home = bridge_home
        self.phase = self.PHASE_LANG
        self.langs = load_languages(bridge_home)
        self.selected_lang: dict | None = None
        self.l_code = "en"
        self.engine: str | None = None
        self.or_key: str | None = None

    def get_prompt_text(self) -> str:
        if self.phase == self.PHASE_LANG:
            lines = [
                "\U0001f30e HASHI \u2014 Welcome / \u3088\u3046\u3053\u305d / \u6b22\u8fce",
                "",
                "Select your language:",
            ]
            for i, lang in enumerate(self.langs, 1):
                lines.append(f"  [{i}] {lang.get('displayName', f'Language {i}')}")
            lines.append("")
            lines.append("Enter number:")
            return "\n".join(lines)

        if self.phase == self.PHASE_DISCLAIMER:
            disc_path = self.bridge_home / "onboarding" / "languages" / f"disclaimer_{self.l_code}.md"
            if not disc_path.exists():
                disc_path = self.bridge_home / "onboarding" / "languages" / "disclaimer_en.md"
            text = disc_path.read_text(encoding="utf-8") if disc_path.exists() else "(Disclaimer not found)"
            return text + '\n\nType "I AGREE" to continue:'

        if self.phase == self.PHASE_AUDIT:
            return "\U0001f50d Detecting available backends..."

        if self.phase == self.PHASE_OPENROUTER:
            return "No local CLI found. Please enter your OpenRouter API key:"

        return ""

    def handle_input(self, text: str) -> tuple[str, bool]:
        """Process user input. Returns (message_to_display, needs_more_input)."""
        if self.phase == self.PHASE_LANG:
            try:
                idx = int(text.strip()) - 1
                if 0 <= idx < len(self.langs):
                    self.selected_lang = self.langs[idx]
                    self.l_code = lang_code_from_file(self.selected_lang.get("_file", ""))
                    self.phase = self.PHASE_DISCLAIMER
                    return (f"\u2705 Language: {self.selected_lang.get('displayName', '?')}\n\n"
                            + self.get_prompt_text()), True
                return "Invalid number. Try again.", True
            except ValueError:
                return "Please enter a number.", True

        if self.phase == self.PHASE_DISCLAIMER:
            if text.strip() == "I AGREE":
                self.phase = self.PHASE_AUDIT
                return "\u2705 Accepted.\n\n" + self.get_prompt_text(), False  # auto-continue to audit
            return 'You must type "I AGREE" exactly.', True

        if self.phase == self.PHASE_OPENROUTER:
            key = text.strip()
            if not key:
                return "Key cannot be empty.", True
            if verify_openrouter(key):
                self.or_key = key
                self.engine = "openrouter-api"
                self.phase = self.PHASE_DONE
                return "\u2705 OpenRouter key verified!", False
            return "\u274c Invalid key. Try again:", True

        return "", False

    def run_audit(self) -> str:
        """Blocking call — detect CLIs. Call from a worker thread."""
        cli_name, engine = audit_environment()
        if engine:
            self.engine = engine
            self.phase = self.PHASE_DONE
            return f"\u2705 Detected: {cli_name} ({engine})"
        self.phase = self.PHASE_OPENROUTER
        return "\u26a0\ufe0f No local CLI backend found."

    def finalize(self):
        """Write agents.json + secrets.json."""
        if self.selected_lang and self.engine:
            write_config(self.bridge_home, self.engine, self.selected_lang, self.l_code, self.or_key)


# ── Main App ────────────────────────────────────────────────────────────────

class HASHITuiApp(App):
    TITLE = "HASHI \u30cf\u30b7 \u6a4b"
    CSS = """
    Screen {
        background: #050b12;
        color: #dff6ff;
    }
    Screen > .screen--selection {
        background: #ffdf6b;
        color: #050b12;
    }
    #main-container {
        height: 1fr;
        padding: 0 1 1 1;
    }
    #content-container {
        height: 1fr;
    }
    #primary-pane {
        width: 1fr;
        height: 1fr;
    }
    #log-panel {
        height: 1fr;
        min-height: 4;
    }
    #chat-container {
        height: 3fr;
        min-height: 10;
    }
    #footer-info-box {
        height: auto;
        min-height: 4;
    }
    """

    BINDINGS = [
        Binding("ctrl+l", "toggle_log_pause", "Pause Log", show=True),
        Binding("tab", "complete_command", "Complete command", show=False, priority=True),
        Binding("ctrl+q", "quit_app", "Quit", show=True),
    ]

    def __init__(
        self,
        workbench_url: str = DEFAULT_WORKBENCH_LOCALHOST_URL,
        onboarding_mode: bool = False,
        *,
        workbench_urls: list[str] | tuple[str, ...] | None = None,
        bridge_home: Path | None = None,
        code_root: Path | None = None,
        launch_instance_id: str | None = None,
    ):
        super().__init__()
        self.bridge_home = Path(bridge_home).resolve() if bridge_home else self._find_bridge_home()
        self.code_root = Path(code_root).resolve() if code_root else Path(__file__).resolve().parent.parent
        configured_instance_id, _workbench_port = load_launch_instance(self.bridge_home)
        self.launch_instance_id = str(launch_instance_id or configured_instance_id).strip().upper()
        local_urls = list(workbench_urls or [workbench_url])
        if workbench_url not in local_urls:
            local_urls.insert(0, workbench_url)
        self.current_instance_id = self.launch_instance_id
        self.bridge_proc: asyncio.subprocess.Process | None = None
        self.current_agent: str | None = None
        self._chat_targets: list[str] = []
        self.current_agent_display: str = ""
        self.current_backend: str = ""
        self._current_agent_metadata: dict = {}
        self._tui_client_id = f"tui-{uuid4().hex}"
        self._active_run_ref: tuple[int, str, str, str, str] | None = None
        self._active_run_phase = "idle"
        self._submission_sequence = 0
        self._latest_submission_ref: tuple[int, str, int] | None = None
        preferences = self._load_tui_preferences()
        requested_layout = str(
            os.environ.get("HASHI_TUI_LAYOUT") or preferences.get("layout") or "chat"
        ).casefold()
        self._layout_mode = requested_layout if requested_layout in {"chat", "balanced", "compact"} else "chat"
        requested_language = str(
            os.environ.get("HASHI_TUI_LANGUAGE") or preferences.get("language") or "en"
        ).casefold()
        self._ui_language = "zh" if requested_language.startswith("zh") else "en"
        requested_sounds = os.environ.get("HASHI_TUI_SOUNDS")
        if requested_sounds is None:
            self._sound_enabled = bool(preferences.get("sounds", True))
        else:
            self._sound_enabled = requested_sounds.strip().casefold() not in {
                "0", "false", "no", "off",
            }
        self._tui_typing_enabled = _enabled_setting(
            os.environ.get("HASHI_TUI_TYPING", preferences.get("typing")),
            default=True,
        )
        self._telegram_mirror_enabled = _enabled_setting(
            os.environ.get(
                "HASHI_TUI_TELEGRAM_MIRROR",
                preferences.get("telegram_mirror"),
            ),
            default=True,
        )
        self._side_panel_enabled = _enabled_setting(
            os.environ.get("HASHI_TUI_SIDEPANEL", preferences.get("sidepanel")),
            default=False,
        )
        self._side_panel_auto_scroll = _enabled_setting(
            os.environ.get(
                "HASHI_TUI_SIDEPANEL_AUTO",
                preferences.get("sidepanel_auto"),
            ),
            default=False,
        )
        command_names = [f"/{spec.name}" for spec in COMMAND_SPECS if spec.menu_visible]
        command_names.extend(f"/{name}" for name in TUI_COMMAND_HELP)
        self._command_names = list(dict.fromkeys(command_names))
        self._command_specs = {spec.name: spec for spec in COMMAND_SPECS}
        known_command_names = [f"/{spec.name}" for spec in COMMAND_SPECS]
        known_command_names.extend(f"/{name}" for name in TUI_COMMAND_HELP)
        self._known_command_names = list(dict.fromkeys(known_command_names))
        self._command_order = {
            command: index for index, command in enumerate(self._command_names)
        }
        self._suggestion_names = sorted(
            self._command_names,
            key=lambda command: (len(command), self._command_order[command]),
        )
        self._current_command_match: str | None = None
        self._command_matches: list[str] = []
        self._command_match_index = 0
        self._parameter_command_name: str | None = None
        self._parameter_path: tuple[str, ...] = ()
        self.api = TuiApiClient(
            base_url=local_urls[0],
            fallback_base_urls=local_urls[1:],
            expected_instance_id=self.launch_instance_id,
        )
        self._instance_resolver = InstanceResolver(self.bridge_home, local_urls)
        self._instance_switch_lock = asyncio.Lock()
        self._connection_generation = 0
        self.gateway_ok = False
        self._log_paused = False
        self._onboarding: OnboardingPhase | None = None
        self._light_onboarding: LightOnboardingPhase | None = None
        self._inject_wakeup: str | None = None
        self._onboarding_mode = onboarding_mode
        self._poll_task: asyncio.Task | None = None
        self._log_follow_task: asyncio.Task | None = None
        self._agents_cache: list[dict] = []
        self._attached_log_path: Path | None = None
        self._agent_refresh_tick = 0
        self._startup_task: asyncio.Task | None = None
        self._side_panel_refresh_task: asyncio.Task | None = None
        self._side_panel_overview: dict | None = None
        self._side_panel_scheduler_jobs: list[dict] | None = None
        self._side_panel_background_jobs: list[dict] | None = None
        self._side_panel_data_agent: str | None = None
        self._side_panel_loading = False
        self._side_panel_incomplete = False

    def _find_bridge_home(self) -> Path:
        env = os.environ.get("BRIDGE_HOME")
        if env:
            return Path(env).resolve()
        # Walk up from this file to find main.py
        candidate = Path(__file__).resolve().parent.parent
        if (candidate / "main.py").exists():
            return candidate
        return Path.cwd()

    def copy_to_clipboard(self, text: str) -> None:
        """Use Textual OSC 52 and the native Windows clipboard when present."""

        super().copy_to_clipboard(text)
        if not copy_to_windows_clipboard(text):
            logger.debug("Native Windows clipboard adapter was unavailable; OSC 52 was used")

    @property
    def _preferences_path(self) -> Path:
        return self.bridge_home / "state" / "tui_preferences.json"

    def _load_tui_preferences(self) -> dict:
        try:
            data = json.loads(self._preferences_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, OSError, ValueError):
            return {}

    def _save_tui_preferences(self):
        try:
            self._preferences_path.parent.mkdir(parents=True, exist_ok=True)
            self._preferences_path.write_text(
                json.dumps(
                    {
                        "language": self._ui_language,
                        "layout": self._layout_mode,
                        "sounds": self._sound_enabled,
                        "typing": self._tui_typing_enabled,
                        "telegram_mirror": self._telegram_mirror_enabled,
                        "sidepanel": self._side_panel_enabled,
                        "sidepanel_auto": self._side_panel_auto_scroll,
                    },
                    ensure_ascii=False,
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("TUI preferences could not be saved: error=%s", exc)

    def compose(self) -> ComposeResult:
        with Vertical(id="main-container"):
            with Horizontal(id="content-container"):
                with Vertical(id="primary-pane"):
                    yield LogPanel(id="log-panel")
                    with Vertical(id="chat-container"):
                        yield ChatHistory(id="chat-history")
                        yield TypingIndicator(id="typing-indicator")
                        yield ChatInput(
                            placeholder="Message · /help · /to <agent> · /instance",
                            suggester=SuggestFromList(self._suggestion_names, case_sensitive=False),
                            id="chat-input",
                        )
                        yield CommandPreview(id="command-preview")
                yield SidePanel(
                    id="side-panel",
                    auto_scroll=self._side_panel_auto_scroll,
                )
            yield FooterInfoBox(id="footer-info-box")

    def on_mount(self):
        self._apply_layout(self._layout_mode, persist=False)
        self._apply_side_panel_visibility(persist=False)
        self._refresh_chrome()
        # Start the intro only after the first screen refresh so frames are visible.
        self.call_after_refresh(self._schedule_startup_sequence)

    def _schedule_startup_sequence(self):
        if self._startup_task and not self._startup_task.done():
            return
        self._startup_task = asyncio.create_task(self._run_startup_sequence())

    async def _run_startup_sequence(self):
        await asyncio.sleep(0.05)
        await self._play_log_startup_animation()
        await asyncio.sleep(0.1)
        agents_path = self.bridge_home / "agents.json"
        needs_onboarding = True
        if agents_path.exists():
            try:
                cfg = json.loads(agents_path.read_text(encoding="utf-8-sig"))
                if cfg.get("agents"):
                    needs_onboarding = False
            except Exception as exc:
                logger.warning("TUI startup config unreadable: path=%s error=%s", agents_path, exc)

        if self._onboarding_mode and not is_onboarding_complete(self.bridge_home):
            self._start_light_onboarding()
        elif needs_onboarding:
            self._start_onboarding()
        else:
            self._start_bridge()

    async def _play_log_startup_animation(self):
        self._render_host_header()
        await asyncio.sleep(0.2)

    def _render_host_header(self):
        log = self.query_one("#log-panel", LogPanel)
        log.clear()
        if self._layout_mode == "balanced":
            colors = ("#71b7ff", "#7dc6ff", "#87d4ff", "#92e1ff", "#9eeed8", "#c7ff8a")
            logo = "\n".join(
                f"[bold {color}]{line}[/]" for color, line in zip(colors, STARTUP_LOGO)
            )
            status = "终端已连接" if self._ui_language == "zh" else "Terminal connected"
            log.write(markup(f"{logo}\n[#9be7ff]{self.launch_instance_id} · {status}[/]"))
        else:
            status = (
                "终端已连接 · 正在准备本地服务…"
                if self._ui_language == "zh"
                else "Terminal connected · preparing local services…"
            )
            log.write(markup(
                f"[bold #63ffd9]HASHI · {self.launch_instance_id}[/]\n[#9be7ff]{status}[/]"
            ))

    # ── Onboarding ──────────────────────────────────────────────────────

    def _start_onboarding(self):
        self._onboarding = OnboardingPhase(self.bridge_home)
        chat = self.query_one("#chat-history", ChatHistory)
        chat.border_title = "Onboarding"
        chat.write(self._onboarding.get_prompt_text())
        self.query_one("#chat-input", ChatInput).placeholder = "Enter your choice..."
        self._update_status_bar()

    async def _handle_onboarding_input(self, text: str):
        chat = self.query_one("#chat-history", ChatHistory)
        ob = self._onboarding

        msg, needs_input = ob.handle_input(text)
        if msg:
            chat.write(msg)

        if ob.phase == OnboardingPhase.PHASE_AUDIT:
            # Run blocking audit in thread
            self._run_audit_in_thread()
            return

        if ob.phase == OnboardingPhase.PHASE_DONE:
            chat.write("\n\U0001f680 Creating configuration...")
            ob.finalize()
            chat.write("\u2705 Configuration created! Starting HASHI...\n")
            self._onboarding = None
            self.query_one("#chat-input", ChatInput).placeholder = "Type message, /to <agent>, or /instance ..."
            self.query_one("#chat-history", ChatHistory).border_title = "Chat"
            self._start_bridge()

    @work(thread=True)
    def _run_audit_in_thread(self):
        result = self._onboarding.run_audit()
        self.call_from_thread(self._audit_complete, result)

    def _audit_complete(self, result: str):
        chat = self.query_one("#chat-history", ChatHistory)
        chat.write(result)

        if self._onboarding.phase == OnboardingPhase.PHASE_OPENROUTER:
            chat.write("\n" + self._onboarding.get_prompt_text())
        elif self._onboarding.phase == OnboardingPhase.PHASE_DONE:
            chat.write("\n\U0001f680 Creating configuration...")
            self._onboarding.finalize()
            chat.write("\u2705 Configuration created! Starting HASHI...\n")
            self._onboarding = None
            self.query_one("#chat-input", ChatInput).placeholder = "Type message, /to <agent>, or /instance ..."
            self.query_one("#chat-history", ChatHistory).border_title = "Chat"
            self._start_bridge()

    # ── Light Onboarding (TUI_onboarding first-run flow) ─────────────────

    def _start_light_onboarding(self):
        self._light_onboarding = LightOnboardingPhase(self.bridge_home)
        chat = self.query_one("#chat-history", ChatHistory)
        chat.border_title = "Setup"
        chat.write(self._light_onboarding.get_initial_prompt())
        self.query_one("#chat-input", ChatInput).placeholder = "Enter your choice..."

    async def _handle_light_onboarding_input(self, text: str):
        chat = self.query_one("#chat-history", ChatHistory)
        lo = self._light_onboarding

        msg, needs_input = lo.handle_input(text)
        if msg:
            chat.write(msg)

        if lo.phase == LightOnboardingPhase.PHASE_APICHECK:
            self._run_light_api_check()
            return

        if lo.phase == LightOnboardingPhase.PHASE_DONE:
            self._finalize_light_onboarding()

    @work(thread=True)
    def _run_light_api_check(self):
        result, ok = self._light_onboarding.run_api_check()
        self.call_from_thread(self._light_api_check_complete, result, ok)

    def _light_api_check_complete(self, result: str, ok: bool):
        chat = self.query_one("#chat-history", ChatHistory)
        chat.write(result)
        if self._light_onboarding.phase == LightOnboardingPhase.PHASE_APIKEY_INPUT:
            pass  # wait for user input
        elif self._light_onboarding.phase == LightOnboardingPhase.PHASE_DONE:
            self._finalize_light_onboarding()

    def _finalize_light_onboarding(self):
        chat = self.query_one("#chat-history", ChatHistory)
        lo = self._light_onboarding
        lo.finalize()
        self._inject_wakeup = lo.get_wakeup_prompt()
        self._light_onboarding = None
        chat.write("✅ Setup complete! Starting HASHI...\n")
        self.query_one("#chat-input", ChatInput).placeholder = "Type message, /to <agent>, or /instance ..."
        self.query_one("#chat-history", ChatHistory).border_title = "Chat"
        self._start_bridge()

    # ── Bridge subprocess ───────────────────────────────────────────────

    def _start_bridge(self):
        self._launch_bridge_task()

    @work()
    async def _launch_bridge_task(self):
        self._attached_log_path = self._resolve_attach_log_path()

        # If API is already up (HASHI already running), just attach — don't start a new process
        if await self.api.health():
            self._write_log_line(
                f"[TUI] {self.launch_instance_id} is already running — attaching to launch instance."
            )
            self._start_attached_log_follow()
            self.gateway_ok = True
            await self._load_agents(client=self.api, generation=self._connection_generation)
            self._start_polling()
            self._update_status_bar()
            return

        attach_only = str(os.environ.get("HASHI_TUI_ATTACH_ONLY") or "").strip().casefold()
        if attach_only in {"1", "true", "yes", "on"}:
            self._write_log_line(
                f"[TUI] Waiting for the launched {self.launch_instance_id} local API."
            )
            self._start_attached_log_follow()
            await self._wait_for_api()
            return

        self._write_log_line("[TUI] Starting HASHI main process...")

        command = [
            sys.executable,
            str(self.code_root / "main.py"),
            "--bridge-home",
            str(self.bridge_home),
        ]
        enable_gateway = str(
            os.environ.get("HASHI_TUI_ENABLE_API_GATEWAY", "1")
        ).strip().casefold() not in {"0", "false", "no", "off"}
        if enable_gateway:
            command.append("--api-gateway")
        self.bridge_proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(self.code_root),
        )

        # Stream stdout to log panel
        asyncio.create_task(self._stream_logs())

        # Wait for API to become available then start polling
        await self._wait_for_api()

    async def _stream_logs(self):
        while self.bridge_proc and self.bridge_proc.returncode is None:
            try:
                line = await self.bridge_proc.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                if decoded and not self._log_paused:
                    self._write_log_line(decoded)
            except Exception as exc:
                logger.warning("TUI bridge log stream failed: error=%s", exc)
                break
        self._write_log_line("[TUI] Bridge process exited.")

    def _resolve_attach_log_path(self) -> Path | None:
        for candidate in (
            self.bridge_home / "logs" / "bridge.log",
            self.bridge_home / "bridge_launch.log",
            self.bridge_home / "logs" / "bridge_launch.log",
            self.bridge_home / "bin" / "bridge_launch.log",
        ):
            if candidate.exists():
                return candidate
        return None

    def _start_attached_log_follow(self):
        if self._log_follow_task and not self._log_follow_task.done():
            return
        self._log_follow_task = asyncio.create_task(self._follow_attached_log())

    async def _follow_attached_log(self):
        path = self._attached_log_path
        if not path:
            self._write_log_line("[TUI] No attachable log file found for the running HASHI instance.")
            return

        try:
            position = path.stat().st_size
            self._write_log_line(f"[TUI] Following launcher log from now: {path}")
        except Exception as exc:
            self._write_log_line(f"[TUI] Failed to read attach log {path}: {exc}")
            return

        while self.bridge_proc is None:
            try:
                if not path.exists():
                    await asyncio.sleep(1.0)
                    continue
                current_size = path.stat().st_size
                if current_size < position:
                    position = 0
                if current_size > position:
                    chunk, position = await asyncio.to_thread(self._read_log_chunk, path, position)
                    for line in chunk:
                        if line and not self._log_paused:
                            self._write_log_line(line)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("TUI attached log follow failed: path=%s error=%s", path, exc)
            await asyncio.sleep(1.0)

    def _read_log_chunk(self, path: Path, position: int) -> tuple[list[str], int]:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(position)
            lines = [line.rstrip() for line in fh.readlines()]
            return lines, fh.tell()

    def _write_log_line(self, text: str):
        log = self.query_one("#log-panel", LogPanel)
        log.write(Text.from_ansi(text))

    async def _wait_for_api(self):
        for attempt in range(60):
            if await self.api.health():
                self.gateway_ok = True
                self._write_log_line("[TUI] Local HASHI API connected.")
                await self._load_agents(client=self.api, generation=self._connection_generation)
                self._start_polling()
                self._update_status_bar()
                return
            await asyncio.sleep(1)
        self._write_log_line("[TUI] Warning: local HASHI API not reachable after 60s. Chat disabled.")
        self._update_status_bar()

    async def _load_agents(
        self,
        *,
        client: TuiApiClient | None = None,
        generation: int | None = None,
        agents: list[dict] | None = None,
    ):
        client = client or self.api
        generation = self._connection_generation if generation is None else generation
        agents = await client.list_agents() if agents is None else agents
        if generation != self._connection_generation or client is not self.api:
            logger.debug("Discarded stale TUI agent load: generation=%s", generation)
            return
        self._adopt_agent_directory(agents)
        self._update_status_bar()
        if agents and not self.current_agent:
            # Auto-select first active agent
            for a in agents:
                if a.get("is_active") or a.get("online"):
                    self._select_agent(a, client=client, generation=generation)
                    break
            if not self.current_agent and agents:
                self._select_agent(agents[0], client=client, generation=generation)

    def _adopt_agent_directory(self, agents: list[dict]) -> None:
        self._agents_cache = list(agents)
        if not self.current_agent:
            self._render_side_panel()
            return
        selected = next(
            (item for item in agents if item.get("name") == self.current_agent),
            None,
        )
        if selected is None:
            self._render_side_panel()
            return
        self._current_agent_metadata = dict(selected)
        self.current_agent_display = str(
            selected.get("display_name") or self.current_agent
        )
        self.current_backend = str(
            selected.get("active_backend") or selected.get("engine") or ""
        )
        self._render_side_panel()
        self._schedule_side_panel_refresh()

    def _select_agent(
        self,
        agent_data: dict,
        *,
        client: TuiApiClient | None = None,
        generation: int | None = None,
    ):
        client = client or self.api
        generation = self._connection_generation if generation is None else generation
        selected_name = agent_data.get("name", "")
        if selected_name != self.current_agent:
            self._clear_typing_indicator()
            self._latest_submission_ref = None
            self._cancel_side_panel_refresh()
            self._reset_side_panel_data()
        self.current_agent = selected_name
        self._chat_targets = [self.current_agent] if self.current_agent else []
        self.current_agent_display = agent_data.get("display_name", self.current_agent)
        self.current_backend = agent_data.get("active_backend", agent_data.get("engine", ""))
        self._current_agent_metadata = dict(agent_data)
        client.reset_offset(self.current_agent)
        chat = self.query_one("#chat-history", ChatHistory)
        emoji = agent_data.get("emoji", "")
        location = self._location_label()
        chat_label = "聊天" if self._ui_language == "zh" else "Chat"
        chat.border_title = (
            f"{chat_label} · {emoji} {self.current_agent_display} ({self.current_agent})"
            f"@{self.current_instance_id} ({location})"
        )
        self._update_status_bar()
        self._render_side_panel()
        self._schedule_side_panel_refresh()
        # Load recent transcript
        self._load_initial_transcript(client, self.current_agent, generation)

        # First-run wakeup: send once after initial agent selection
        if self._inject_wakeup and self.current_agent:
            wakeup = self._inject_wakeup
            self._inject_wakeup = None
            self._send_wakeup(wakeup, self.current_agent, client, generation)

    @work()
    async def _send_wakeup(
        self,
        prompt: str,
        agent: str,
        client: TuiApiClient,
        generation: int,
    ):
        await asyncio.sleep(2.0)  # let bridge settle
        if generation != self._connection_generation or client is not self.api:
            logger.info("Skipped stale onboarding wakeup: agent=%s generation=%s", agent, generation)
            return
        await client.send_chat(
            agent,
            prompt,
            client_id=self._tui_client_id,
            telegram_mirror=self._telegram_mirror_enabled,
            ui_locale=self._ui_language,
        )

    def _render_transcript_message(self, msg: dict):
        role = msg.get("role", "?")
        text = msg.get("text", "")
        if not text:
            return

        chat = self.query_one("#chat-history", ChatHistory)
        source = msg.get("agent") or msg.get("agent_id") or msg.get("source", "")
        prefix = self.current_agent_display

        if self.current_agent_display == "ALL" and source and source not in {"text", "api", "photo", "system"}:
            prefix = source

        if role == "user":
            chat.write(chat_message_renderable("user", "You", text))
        elif role == "assistant":
            chat.write(chat_message_renderable("assistant", prefix, text))
            self._clear_typing_for_transcript_message(msg)

    @work()
    async def _load_initial_transcript(
        self,
        client: TuiApiClient,
        agent: str,
        generation: int,
    ):
        if not agent:
            return
        messages = await client.get_recent_transcript(agent, limit=20)
        if (
            generation != self._connection_generation
            or client is not self.api
            or agent != self.current_agent
        ):
            logger.debug("Discarded stale TUI transcript load: agent=%s generation=%s", agent, generation)
            return
        chat = self.query_one("#chat-history", ChatHistory)
        chat.clear()
        for msg in messages:
            self._render_transcript_message(msg)

    # ── Transcript polling ──────────────────────────────────────────────

    def _start_polling(self):
        if self._poll_task and not self._poll_task.done():
            return
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self):
        while True:
            if self.gateway_ok:
                generation = self._connection_generation
                client = self.api
                try:
                    self._agent_refresh_tick = (self._agent_refresh_tick + 1) % 5
                    if self._agent_refresh_tick == 0:
                        agents = await client.list_agents()
                        if generation == self._connection_generation and client is self.api:
                            self._adopt_agent_directory(agents)
                            self._update_status_bar()
                    poll_targets = self._chat_targets[:]
                    if self.current_agent and self.current_agent not in poll_targets:
                        poll_targets.append(self.current_agent)
                    for agent in poll_targets:
                        messages = await client.poll_transcript(agent)
                        if generation != self._connection_generation or client is not self.api:
                            logger.debug("Discarded stale TUI poll result: agent=%s generation=%s", agent, generation)
                            break
                        received = False
                        for msg in messages:
                            if msg.get("role") == "assistant":
                                self._render_transcript_message(msg)
                                received = True
                        if received:
                            self._play_message_sound("received")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "TUI transcript polling failed: instance=%s error=%s",
                        self.current_instance_id,
                        exc,
                    )
            await asyncio.sleep(1.0)

    # ── Input handling ──────────────────────────────────────────────────

    async def on_input_submitted(self, event: Input.Submitted):
        text = event.value.strip()

        # Light onboarding accepts empty Enter to advance
        if self._light_onboarding:
            event.input.value = ""
            await self._handle_light_onboarding_input(text)
            return

        if not text:
            event.input.value = ""
            return
        normalized = text
        if normalized.startswith("/ "):
            normalized = "/" + normalized[2:].lstrip()

        # Onboarding mode
        if self._onboarding:
            event.input.value = ""
            await self._handle_onboarding_input(normalized)
            return

        if normalized.startswith("/"):
            resolved = self._resolve_submitted_command(normalized)
            if resolved is None:
                event.input.value = ""
                self._hide_command_preview()
                chat = self.query_one("#chat-history", ChatHistory)
                command = normalized.split(maxsplit=1)[0]
                message = (
                    f"未找到命令：{command}"
                    if self._ui_language == "zh"
                    else f"Unknown command: {command}"
                )
                chat.write(Text(message, style="dim #9fb3c8"))
                return
            normalized = resolved

        event.input.value = ""
        self._hide_command_preview()

        # TUI local commands
        if normalized == "/to" or normalized.startswith("/to "):
            await self._handle_to(normalized)
            return
        if normalized == "/agents":
            await self._handle_agents_cmd()
            return
        if normalized == "/telegram" or normalized.startswith("/telegram "):
            self._handle_telegram_cmd(normalized)
            return
        if normalized == "/sidepanel" or normalized.startswith("/sidepanel "):
            await self._handle_sidepanel_cmd(normalized)
            return
        if normalized == "/tui" or normalized.startswith("/tui "):
            self._handle_tui_cmd(normalized)
            return
        if normalized == "/help" or normalized.startswith("/help "):
            self._handle_help_cmd(normalized)
            return
        if normalized == "/layout" or normalized.startswith("/layout "):
            self._handle_layout_cmd(normalized)
            return
        if normalized == "/instance" or normalized.startswith("/instance "):
            await self._handle_instance_cmd(normalized)
            return
        if normalized == "/clear":
            self.clear_selection()
            chat_history = self.query_one("#chat-history", ChatHistory)
            chat_history.clear()
            chat_history.resume_auto_scroll()
            return
        if normalized == "/quit":
            await self._shutdown()
            return
        if normalized == "/log" or normalized.startswith("/log "):
            self._handle_log_cmd(normalized)
            return

        # Everything else → send to agent
        if not self.gateway_ok:
            chat = self.query_one("#chat-history", ChatHistory)
            chat.write(markup("[#ff7a7a]Local HASHI API not connected. Chat unavailable.[/]"))
            return

        chat = self.query_one("#chat-history", ChatHistory)
        chat.write(chat_message_renderable("user", "You", normalized))

        if self.current_agent_display == "ALL":
            # Broadcast to all active agents
            self._play_message_sound("sent")
            self._send_broadcast(
                normalized,
                self.api,
                self._connection_generation,
                self._telegram_mirror_enabled,
                self._ui_language,
            )
            return

        if not self.current_agent:
            chat.write(markup("[yellow]No agent selected. Use /to <name> first.[/]"))
            return
        else:
            self._play_message_sound("sent")
            self._submission_sequence += 1
            submission_ref = (
                self._connection_generation,
                self.current_agent,
                self._submission_sequence,
            )
            self._latest_submission_ref = submission_ref
            self._send_message(
                normalized,
                self.current_agent,
                self.api,
                self._connection_generation,
                self._telegram_mirror_enabled,
                self._ui_language,
                submission_ref,
            )

    def on_input_changed(self, event: Input.Changed):
        """Show command syntax first, then valid parameter completions."""

        value = event.value.lstrip().casefold()
        preview = self.query_one("#command-preview", CommandPreview)
        self._current_command_match = None
        self._command_matches = []
        self._command_match_index = 0
        self._parameter_command_name = None
        self._parameter_path = ()
        if not value.startswith("/") or len(value) < 2:
            if value == "/":
                self._command_matches = [
                    command for command in TUI_DISCOVERY_COMMANDS if command in self._command_names
                ]
                self._show_command_matches()
            else:
                preview.hide_match()
            return
        command, separator, arguments = value.partition(" ")
        canonical = next(
            (
                candidate
                for candidate in self._known_command_names
                if candidate.casefold() == command
            ),
            None,
        )
        if separator and canonical:
            self._parameter_command_name = canonical[1:]
            matches, path = self._parameter_matches(
                self._parameter_command_name,
                arguments,
            )
            self._parameter_path = path
            self._command_matches = matches
            if self._command_matches:
                self._show_command_matches()
            elif self._command_guide(self._parameter_command_name):
                self._show_parameter_usage(self._parameter_command_name)
            else:
                preview.hide_match()
            return
        if separator:
            preview.hide_match()
            return
        self._command_matches = self._ranked_command_matches(value)
        if not self._command_matches:
            preview.hide_match()
            return
        self._show_command_matches()

    def _ranked_command_matches(self, prefix: str) -> list[str]:
        """Return stable prefix matches, preferring the shortest completion."""

        matches = [command for command in self._command_names if command.startswith(prefix)]
        matches.sort(key=lambda command: (len(command), self._command_order[command]))
        return matches[:5]

    def _resolve_submitted_command(self, text: str) -> str | None:
        """Resolve an exact or partial slash command without fuzzy correction."""

        command, separator, arguments = text.partition(" ")
        prefix = command.casefold()
        if prefix == "/":
            return None
        canonical = next(
            (candidate for candidate in self._known_command_names if candidate.casefold() == prefix),
            None,
        )
        if canonical is None:
            matches = self._ranked_command_matches(prefix)
            if not matches:
                return None
            selected = self._current_command_match
            canonical = selected if selected in matches else matches[0]
        resolved = canonical + (separator + arguments if separator else "")
        selected = self._current_command_match
        if (
            separator
            and self._parameter_command_name == canonical[1:]
            and selected
            and " " in selected
            and selected.casefold().startswith(resolved.casefold())
        ):
            return selected
        return resolved

    def _hide_command_preview(self):
        self._command_matches = []
        self._current_command_match = None
        self._command_match_index = 0
        self._parameter_command_name = None
        self._parameter_path = ()
        self.query_one("#command-preview", CommandPreview).hide_match()

    def _show_command_matches(self):
        if not self._command_matches:
            self.query_one("#command-preview", CommandPreview).hide_match()
            self._current_command_match = None
            return
        self._command_match_index %= len(self._command_matches)
        self._current_command_match = self._command_matches[self._command_match_index]
        local_names = set(TUI_COMMAND_HELP)
        tui_scope = "TUI"
        agent_scope = "Agent"
        window_start = min(
            max(0, self._command_match_index - 4),
            max(0, len(self._command_matches) - 5),
        )
        visible_matches = self._command_matches[window_start : window_start + 5]
        matches: list[tuple[str, str, str]] = []
        if self._parameter_command_name:
            name = self._parameter_command_name
            for command in visible_matches:
                matches.append(
                    (
                        command,
                        self._parameter_description(name, command),
                        tui_scope if name in local_names else agent_scope,
                    )
                )
        else:
            for command in visible_matches:
                name = command[1:]
                matches.append(
                    (
                        self._command_usage(name),
                        self._command_description(name),
                        tui_scope if name in local_names else agent_scope,
                    )
                )
        detail_name = self._parameter_command_name or self._current_command_match[1:]
        self.query_one("#command-preview", CommandPreview).show_matches(
            matches,
            self._command_match_index - window_start,
            self._command_guide_details(detail_name),
        )

    def _command_description(self, name: str) -> str:
        local = TUI_COMMAND_HELP.get(name)
        if local:
            return local[0 if self._ui_language == "zh" else 1]
        spec = self._command_specs.get(name)
        if spec is None:
            return ""
        locale = "zh-CN" if self._ui_language == "zh" else "en"
        return ui_language.command_description(name, spec.description, locale=locale)

    def _command_guide(self, name: str) -> CommandGuide | None:
        local = TUI_COMMAND_GUIDES.get(name)
        if local:
            return local
        spec = self._command_specs.get(name)
        if spec is None:
            return None
        if spec.alias_of:
            owner = self._command_specs.get(spec.alias_of)
            return owner.guide if owner else None
        return spec.guide

    def _command_usage(self, name: str) -> str:
        guide = self._command_guide(name)
        return guide.usage if guide else f"/{name}"

    @staticmethod
    def _dedupe_options(values) -> list[str]:
        choices: list[str] = []
        for value in values:
            option = str(value or "").strip()
            if option and option not in choices:
                choices.append(option)
        return choices

    def _runtime_selection(self) -> tuple[str, str, dict, list[dict]]:
        facts = self._current_agent_metadata
        presentation = facts.get("presentation_status")
        presentation = dict(presentation) if isinstance(presentation, dict) else {}
        engine = canonical_backend_engine(
            presentation.get("engine")
            or facts.get("active_backend")
            or facts.get("engine")
        )
        model = str(
            presentation.get("model") or facts.get("model") or ""
        ).strip()
        raw_backends = facts.get("allowed_backends")
        backends = [
            dict(item)
            for item in raw_backends or []
            if isinstance(item, dict)
        ]
        return engine, model, presentation, backends

    def _command_choices(
        self,
        name: str,
        *,
        path: tuple[str, ...] = (),
    ) -> list[str]:
        nested = TUI_NESTED_CHOICES.get((name, *path))
        if nested is not None:
            return list(nested)
        guide = self._command_guide(name)
        if guide is None:
            return []
        if not guide.choice_source:
            return list(guide.choices)

        engine, model, presentation, backends = self._runtime_selection()
        if guide.choice_source == "agents":
            rows = [row for row in self._agents_cache if isinstance(row, dict)]
            if name == "start":
                rows = [
                    row
                    for row in rows
                    if row.get("is_active", row.get("isActive", True))
                    and not row.get("online")
                ]
            choices = [row.get("name") or row.get("id") for row in rows]
            choices.extend(self._chat_targets)
            if name == "to":
                choices.append("all")
            return self._dedupe_options(choices)
        if guide.choice_source == "backends":
            return self._dedupe_options(
                [
                    item.get("engine")
                    for item in backends
                    if is_selectable_backend(item.get("engine"))
                ]
                + [engine]
            )
        if guide.choice_source == "models":
            if engine == HER_V2_ENGINE:
                return [
                    "quick",
                    "pro",
                    "routes",
                    "route",
                    "reasoning",
                    "apply",
                    "discard",
                    "compact",
                ]
            choices = [model]
            for item in backends:
                if canonical_backend_engine(item.get("engine")) != engine:
                    continue
                configured = item.get("models")
                if isinstance(configured, list):
                    choices.extend(configured)
                choices.extend((item.get("model"), item.get("default_model")))
            if engine:
                choices.extend(get_available_models(engine))
            return self._dedupe_options(choices)
        if guide.choice_source == "efforts":
            try:
                return self._dedupe_options(
                    get_available_efforts(
                        engine,
                        model or None,
                        allowed_backends=backends,
                    )
                )
            except (KeyError, TypeError, ValueError):
                return []
        if guide.choice_source == "providers":
            if engine != HER_V2_ENGINE:
                return []
            choices = [
                "hybrid",
                self._current_agent_metadata.get("provider"),
                *[
                    item.get("engine")
                    for item in backends
                    if not is_selectable_backend(item.get("engine"))
                ],
            ]
            her = presentation.get("her_v2")
            if isinstance(her, dict):
                for slot in ("quick", "pro"):
                    target = her.get(slot)
                    if isinstance(target, dict):
                        choices.append(target.get("provider"))
            return self._dedupe_options(choices)
        return list(guide.choices)

    def _parameter_matches(
        self,
        name: str,
        arguments: str,
    ) -> tuple[list[str], tuple[str, ...]]:
        trailing_space = arguments.endswith(" ")
        tokens = arguments.split()
        if trailing_space:
            path = tuple(token.casefold() for token in tokens)
            partial = ""
        elif tokens:
            path = tuple(token.casefold() for token in tokens[:-1])
            partial = tokens[-1].casefold()
        else:
            path = ()
            partial = ""

        choices = self._command_choices(name, path=path)
        if not choices and path:
            return [], path
        prefix = f"/{name}"
        if path:
            prefix += " " + " ".join(path)
        matches = [
            f"{prefix} {choice}"
            for choice in choices
            if choice.casefold().startswith(partial)
        ]
        return matches, path

    def _current_choice(self, name: str, path: tuple[str, ...] = ()) -> str | None:
        engine, model, presentation, _backends = self._runtime_selection()
        if name == "backend":
            return engine or None
        if name == "model":
            return model or None
        if name == "effort":
            return str(presentation.get("effort") or "").strip() or None
        if name == "mode":
            return str(self._current_agent_metadata.get("mode") or "").strip() or None
        if name == "layout":
            return self._layout_mode
        if name == "sidepanel":
            if path == ("auto",):
                return "on" if self._side_panel_auto_scroll else "off"
            return "on" if self._side_panel_enabled else "off"
        if name == "telegram" or (name == "tui" and path == ("telegram",)):
            return "on" if self._telegram_mirror_enabled else "off"
        if name == "tui" and path == ("language",):
            return self._ui_language
        if name == "tui" and path == ("sound",):
            return "on" if self._sound_enabled else "off"
        if name == "tui" and path == ("typing",):
            return "on" if self._tui_typing_enabled else "off"
        return None

    def _parameter_description(self, name: str, completion: str) -> str:
        tokens = completion.split()
        option = tokens[-1] if tokens else ""
        path = tuple(token.casefold() for token in tokens[1:-1])
        current = self._current_choice(name, path)
        if current and option.casefold() == current.casefold():
            return "当前选择" if self._ui_language == "zh" else "Current selection"
        if name == "sidepanel" and path == ("auto",):
            descriptions = {
                "zh": {
                    "on": "开启自动巡览",
                    "off": "关闭自动巡览",
                    "toggle": "切换自动巡览",
                },
                "en": {
                    "on": "Enable automatic tour",
                    "off": "Disable automatic tour",
                    "toggle": "Toggle automatic tour",
                },
            }
            if option.casefold() in descriptions[self._ui_language]:
                return descriptions[self._ui_language][option.casefold()]
        explanations = {
            "zh": {
                "low": "较少推理，响应更快",
                "medium": "平衡速度与推理",
                "high": "更深入地处理困难任务",
                "xhigh": "很高的推理强度",
                "max": "最高推理强度",
                "fixed": "保持连续的模型会话",
                "flex": "每轮重新注入完整上下文",
                "on": "开启",
                "off": "关闭",
                "status": "查看当前状态",
                "full": "显示完整详情",
                "all": "全部",
                "more": "显示更多详情",
                "show": "显示",
                "list": "列出可用项目",
                "refresh": "刷新",
                "reset": "恢复默认设置",
                "pause": "暂停",
                "resume": "继续",
                "run": "立即运行",
                "stop": "停止",
                "add": "新增",
                "delete": "删除",
                "create": "创建",
                "edit": "编辑",
                "help": "查看帮助",
                "find": "查找",
                "enable": "启用",
                "disable": "停用",
                "validate": "验证",
                "apply": "应用更改",
                "discard": "放弃更改",
                "compact": "压缩或整理",
                "quiet": "仅显示必要信息",
                "activity": "显示执行活动",
                "raw": "显示原始详情",
                "summary": "显示汇总",
                "test": "播放测试",
                "language": "设置 TUI 语言",
                "sound": "设置 TUI 声音",
                "typing": "设置输入提示",
                "telegram": "设置 Telegram 镜像",
                "auto": "设置自动巡览",
                "toggle": "切换当前状态",
                "chat": "聊天优先布局",
                "balanced": "均衡布局",
                "hide": "隐藏",
                "zh": "简体中文",
                "en": "English",
            },
            "en": {
                "low": "Less reasoning, faster responses",
                "medium": "Balanced speed and reasoning",
                "high": "Deeper reasoning for difficult tasks",
                "xhigh": "Very high reasoning effort",
                "max": "Maximum reasoning effort",
                "fixed": "Persistent engine session",
                "flex": "Re-inject full context each turn",
                "on": "Enable",
                "off": "Disable",
                "status": "Show current status",
                "full": "Show full details",
                "all": "All",
                "more": "Show more details",
                "show": "Show",
                "list": "List available items",
                "refresh": "Refresh",
                "reset": "Restore defaults",
                "pause": "Pause",
                "resume": "Resume",
                "run": "Run now",
                "stop": "Stop",
                "add": "Add",
                "delete": "Delete",
                "create": "Create",
                "edit": "Edit",
                "help": "Show help",
                "find": "Find",
                "enable": "Enable",
                "disable": "Disable",
                "validate": "Validate",
                "apply": "Apply changes",
                "discard": "Discard changes",
                "compact": "Compact or organize",
                "quiet": "Essential messages only",
                "activity": "Show execution activity",
                "raw": "Show raw detail",
                "summary": "Show summary",
                "test": "Play a test",
                "language": "Set TUI language",
                "sound": "Set TUI sounds",
                "typing": "Set typing indicator",
                "telegram": "Set Telegram mirroring",
                "auto": "Configure automatic tour",
                "toggle": "Toggle the current state",
                "chat": "Chat-first layout",
                "balanced": "Balanced layout",
                "hide": "Hide",
                "zh": "Simplified Chinese",
                "en": "English",
            },
        }
        localized = explanations[self._ui_language]
        if option.casefold() in localized:
            return localized[option.casefold()]
        if name == "backend":
            return "可用后端" if self._ui_language == "zh" else "Available backend"
        if name == "model":
            return "可用模型" if self._ui_language == "zh" else "Available model"
        if name in {"to", "start", "transfer", "fork", "hchat"}:
            return "可用 Agent" if self._ui_language == "zh" else "Available Agent"
        return "可用参数" if self._ui_language == "zh" else "Available option"

    def _guide_example(self, name: str, choices: list[str]) -> str | None:
        guide = self._command_guide(name)
        if guide is None:
            return None
        if name == "sidepanel" and self._parameter_path == ("auto",):
            return "/sidepanel auto on"
        localized = TUI_LOCALIZED_EXAMPLES.get(name)
        if localized:
            suffix = localized[0 if self._ui_language == "zh" else 1]
            return f"/{name} {suffix}"
        if name == "hchat" and choices:
            message = "请检查状态" if self._ui_language == "zh" else "please check status"
            return f"/hchat {choices[0]} {message}"
        if guide.example:
            return guide.example
        preferred = "high" if name == "effort" and "high" in choices else None
        selected = preferred or next(iter(choices), None)
        return f"/{name} {selected}" if selected else None

    def _command_guide_details(
        self,
        name: str,
        *,
        include_usage: bool = False,
    ) -> tuple[str, ...]:
        guide = self._command_guide(name)
        if guide is None:
            return ()
        choices = self._command_choices(name, path=self._parameter_path)
        labels = (
            {"usage": "用法", "options": "可选", "example": "示例"}
            if self._ui_language == "zh"
            else {"usage": "Usage", "options": "Options", "example": "Example"}
        )
        details: list[str] = []
        if choices:
            details.append(f"{labels['options']} · {' · '.join(choices)}")
        if include_usage:
            details.append(f"{labels['usage']} · {guide.usage}")
        example = self._guide_example(name, choices)
        if example:
            details.append(f"{labels['example']} · {example}")
        return tuple(details)

    def _show_parameter_usage(self, name: str) -> None:
        scope = "TUI" if name in TUI_COMMAND_HELP else "Agent"
        self.query_one("#command-preview", CommandPreview).show_matches(
            [(self._command_usage(name), self._command_description(name), scope)],
            details=self._command_guide_details(name),
        )

    def action_complete_command(self):
        input_box = self.query_one("#chat-input", ChatInput)
        if self.focused is not input_box or not self._current_command_match:
            return
        input_box.value = self._current_command_match
        input_box.cursor_position = len(input_box.value)
        self.query_one("#command-preview", CommandPreview).hide_match()

    def on_key(self, event):
        """Navigate the visible command palette without affecting ordinary input."""

        if event.key == "escape" and self.screen.get_selected_text() is not None:
            self.clear_selection()
            self.query_one("#chat-history", ChatHistory).resume_auto_scroll()
            event.stop()
            event.prevent_default()
            return
        input_box = self.query_one("#chat-input", ChatInput)
        if self.focused is not input_box or not self._command_matches:
            return
        if event.key == "down":
            self._command_match_index = (self._command_match_index + 1) % len(self._command_matches)
        elif event.key == "up":
            self._command_match_index = (self._command_match_index - 1) % len(self._command_matches)
        elif event.key == "escape":
            self._hide_command_preview()
            event.stop()
            event.prevent_default()
            return
        else:
            return
        self._show_command_matches()
        event.stop()
        event.prevent_default()

    def _handle_tui_cmd(self, text: str):
        chat = self.query_one("#chat-history", ChatHistory)
        parts = text.split()
        if len(parts) >= 2 and parts[1].casefold() == "typing":
            self._handle_tui_typing_cmd(parts[2:])
            return
        if len(parts) >= 2 and parts[1].casefold() == "telegram":
            suffix = " ".join(parts[2:])
            self._handle_telegram_cmd("/telegram" + (f" {suffix}" if suffix else ""))
            return
        if len(parts) == 1:
            current = "中文" if self._ui_language == "zh" else "English"
            sound = (
                ("开" if self._sound_enabled else "关")
                if self._ui_language == "zh"
                else ("on" if self._sound_enabled else "off")
            )
            typing = "ON" if self._tui_typing_enabled else "OFF"
            mirror = "ON" if self._telegram_mirror_enabled else "OFF"
            chat.write(markup(
                f"[#c7ff8a]TUI language · {current} · sound · {sound} · "
                f"typing · {typing} · TG mirror · {mirror}[/]\n"
                "[#9be7ff]/tui language zh|en · /tui sound on|off|test · "
                "/tui typing on|off · /tui telegram on|off[/]"
            ))
            return
        if len(parts) == 2 and parts[1].casefold() in {"language", "lang"}:
            current = "中文" if self._ui_language == "zh" else "English"
            chat.write(markup(
                f"[#c7ff8a]TUI language · {current} · /tui language zh|en[/]"
            ))
            return
        if len(parts) == 3 and parts[1].casefold() in {"language", "lang"}:
            requested = parts[2].casefold()
            if requested in {"zh", "cn", "中文", "chinese"}:
                self._ui_language = "zh"
            elif requested in {"en", "english"}:
                self._ui_language = "en"
            else:
                chat.write(markup("[#ff7a7a]Use /tui language zh|en.[/]"))
                return
            self._save_tui_preferences()
            self._refresh_chrome()
            self._render_host_header()
            message = "✓ TUI 已切换为中文。" if self._ui_language == "zh" else "✓ TUI switched to English."
            chat.write(markup(f"[#63ffd9]{message}[/]"))
            return
        if len(parts) == 2 and parts[1].casefold() == "sound":
            state = (
                ("已开启" if self._sound_enabled else "已关闭")
                if self._ui_language == "zh"
                else ("on" if self._sound_enabled else "off")
            )
            chat.write(markup(
                f"[#c7ff8a]TUI sound · {state} · /tui sound on|off|test[/]"
            ))
            return
        if len(parts) == 3 and parts[1].casefold() == "sound":
            action = parts[2].casefold()
            if action in {"on", "1", "yes"}:
                self._sound_enabled = True
                self._save_tui_preferences()
                played = self._play_message_sound("received")
                message = (
                    "✓ TUI 提示音已开启。" if self._ui_language == "zh"
                    else "✓ TUI sounds enabled."
                )
                if not played:
                    message += (
                        " 当前系统没有可用的音频输出。"
                        if self._ui_language == "zh"
                        else " No audio output is available on this system."
                    )
                chat.write(markup(f"[#63ffd9]{message}[/]"))
                return
            if action in {"off", "0", "no"}:
                self._sound_enabled = False
                self._save_tui_preferences()
                message = (
                    "✓ TUI 提示音已关闭。" if self._ui_language == "zh"
                    else "✓ TUI sounds disabled."
                )
                chat.write(markup(f"[#63ffd9]{message}[/]"))
                return
            if action == "test":
                played = self._play_message_sound("sent")
                if played:
                    self.set_timer(0.2, lambda: self._play_message_sound("received"))
                if played and self._ui_language == "zh":
                    message = "✓ 正在试听发送与接收提示音。"
                elif played:
                    message = "✓ Testing sent and received sounds."
                elif self._ui_language == "zh":
                    message = "当前系统没有可用的音频输出。"
                else:
                    message = "No audio output is available on this system."
                chat.write(markup(f"[#63ffd9]{message}[/]"))
                return
        chat.write(markup(
            "[#ff7a7a]Use /tui language zh|en, /tui sound on|off|test, "
            "/tui typing on|off, or /tui telegram on|off.[/]"
        ))

    def _handle_tui_typing_cmd(self, arguments: list[str]) -> None:
        chat = self.query_one("#chat-history", ChatHistory)
        if not arguments:
            state = "ON" if self._tui_typing_enabled else "OFF"
            message = (
                f"TUI \u8f93\u5165\u63d0\u793a {state}\u3002\u8be5\u8bbe\u7f6e\u4e0e Telegram /typing \u76f8\u4e92\u72ec\u7acb\u3002"
                if self._ui_language == "zh"
                else f"TUI typing indicator {state}. This is independent of Telegram /typing."
            )
            chat.write(Text(message, style="#c7ff8a"))
            return
        action = arguments[0].casefold()
        if len(arguments) != 1 or action not in {"on", "off"}:
            chat.write(Text("Use /tui typing on|off.", style="#ff7a7a"))
            return
        self._tui_typing_enabled = action == "on"
        self._save_tui_preferences()
        self._refresh_typing_indicator()
        message = (
            f"\u2713 TUI \u8f93\u5165\u63d0\u793a\u5df2{'\u5f00\u542f' if self._tui_typing_enabled else '\u5173\u95ed'}\u3002"
            if self._ui_language == "zh"
            else f"\u2713 TUI typing indicator {'enabled' if self._tui_typing_enabled else 'disabled'}."
        )
        chat.write(Text(message, style="#63ffd9"))

    def _handle_telegram_cmd(self, text: str) -> None:
        chat = self.query_one("#chat-history", ChatHistory)
        parts = text.split()
        if len(parts) > 2 or (len(parts) == 2 and parts[1].casefold() not in {"on", "off"}):
            chat.write(Text("Use /telegram on|off.", style="#ff7a7a"))
            return
        changed = len(parts) == 2
        if changed:
            self._telegram_mirror_enabled = parts[1].casefold() == "on"
            self._save_tui_preferences()
            self._update_status_bar()
        state = "ON" if self._telegram_mirror_enabled else "OFF"
        connector = self._current_agent_metadata.get("telegram_connected")
        if self._ui_language == "zh":
            connector_state = (
                "已连接" if connector is True else "离线" if connector is False else "未知"
            )
            prefix = "\u2713 " if changed else ""
            message = (
                f"{prefix}TUI Telegram \u955c\u50cf {state}\uff08Bot {connector_state}\uff09\u3002"
                "\u4ec5\u5f71\u54cd\u6b64 TUI \u4eca\u540e\u63d0\u4ea4\u7684 Run\uff1b\u5f53\u524d\u4f1a\u8bdd\u4ecd\u662f\u6b63\u5f0f\u5171\u4eab\u4f1a\u8bdd\uff0c\u4e0d\u8865\u53d1\u5386\u53f2\u5185\u5bb9\u3002"
            )
        else:
            connector_state = (
                "connected" if connector is True else "offline" if connector is False else "unknown"
            )
            prefix = "\u2713 " if changed else ""
            message = (
                f"{prefix}TUI Telegram mirror {state} (Bot {connector_state}). "
                "This affects only future Runs submitted by this TUI; the Conversation remains shared and no history is replayed."
            )
        chat.write(Text(message, style="#63ffd9" if changed else "#c7ff8a"))

    def _play_message_sound(self, event: str) -> bool:
        return play_message_sound(event, enabled=self._sound_enabled)

    def _handle_help_cmd(self, text: str):
        """Render TUI and common Agent commands without sending them to a model."""

        parts = text.split(maxsplit=1)
        requested = parts[1].strip().casefold() if len(parts) > 1 else ""
        language = "zh" if requested in {"zh", "中文", "chinese"} else self._ui_language
        if requested in {"en", "english"}:
            language = "en"
        chat = self.query_one("#chat-history", ChatHistory)
        if language == "zh":
            help_text = """## ❔ HASHI TUI 帮助

**常用 Agent 命令**

`/backend`　选择或查看后端
`/mode`　选择或查看工作模式
`/status`　查看当前 Agent 状态
`/version [full|all]`　查看实际运行版本

**TUI 导航与布局**

`/to <agent|all>`　切换聊天目标
`/instance [名称]`　查看或切换实例
`/layout [chat|balanced|compact|reset]`　调整窗口比例
`/log [show|hide|pause]`　控制本地日志
`/sidepanel [on|off|toggle|refresh]`　控制只读信息面板
`/sidepanel auto on|off|toggle`　设置自动巡览
`/tui language zh|en`　切换界面语言
`/tui sound on|off|test`　设置或试听提示音
`/clear`　清空当前显示　　`/quit`　退出

输入命令前缀可自动补全；未知命令不会发送给 Agent。输入 `/help en` 查看英文版。"""
        else:
            help_text = """## ❔ HASHI TUI HELP

**Common Agent commands**

`/backend`　Select or inspect the backend
`/mode`　Select or inspect the work mode
`/status`　Show current Agent status
`/version [full|all]`　Show the running version

**TUI navigation and layout**

`/to <agent|all>`　Change the chat target
`/instance [name]`　List or switch instances
`/layout [chat|balanced|compact|reset]`　Resize the panes
`/log [show|hide|pause]`　Control the host log
`/sidepanel [on|off|toggle|refresh]`　Control the read-only information panel
`/sidepanel auto on|off|toggle`　Configure the automatic tour
`/tui language zh|en`　Change the interface language
`/tui sound on|off|test`　Configure or preview message sounds
`/clear`　Clear this view　　`/quit`　Exit

Command prefixes autocomplete; unknown commands are never sent to an Agent. Use `/help zh` for Chinese."""
        if language == "zh":
            help_text += (
                "\n\n`/tui typing on|off`\u3000\u8bbe\u7f6e TUI \u8f93\u5165\u63d0\u793a"
                "\n`/telegram on|off`\u3000\u8bbe\u7f6e\u6b64 TUI \u4eca\u540e Run \u7684 Telegram \u955c\u50cf"
            )
        else:
            help_text += (
                "\n\n`/tui typing on|off`  Configure the TUI typing indicator"
                "\n`/telegram on|off`  Configure Telegram mirroring for future Runs from this TUI"
            )
        chat.write(chat_message_renderable("assistant", "HASHI", help_text))

    def _refresh_chrome(self):
        log = self.query_one("#log-panel", LogPanel)
        chat = self.query_one("#chat-history", ChatHistory)
        input_box = self.query_one("#chat-input", ChatInput)
        self.query_one("#typing-indicator", TypingIndicator).refresh_language(
            self._ui_language
        )
        if self._ui_language == "zh":
            log.border_title = f"主机日志 · {self.launch_instance_id}（本机）"
            input_box.placeholder = "输入消息 · /help · /to <Agent> · /instance"
        else:
            log.border_title = f"Host log · {self.launch_instance_id} (local)"
            input_box.placeholder = "Message · /help · /to <agent> · /instance"
        if self.current_agent_display:
            location = self._location_label()
            chat_label = "聊天" if self._ui_language == "zh" else "Chat"
            emoji = next(
                (str(item.get("emoji") or "") for item in self._agents_cache if item.get("name") == self.current_agent),
                "",
            )
            chat.border_title = (
                f"{chat_label} · {emoji} {self.current_agent_display} ({self.current_agent})"
                f"@{self.current_instance_id} ({location})"
            )
        self._render_side_panel()
        self._update_status_bar()

    def _location_label(self) -> str:
        local = self.current_instance_id == self.launch_instance_id
        if self._ui_language == "zh":
            return "本机" if local else "远程"
        return "local" if local else "remote"

    def _apply_layout(self, mode: str, *, persist: bool = True):
        log = self.query_one("#log-panel", LogPanel)
        chat_container = self.query_one("#chat-container", Vertical)
        if mode == "compact":
            log.display = False
            chat_container.styles.height = "1fr"
        else:
            log.display = True
            log.styles.height = "1fr"
            chat_container.styles.height = "1fr" if mode == "balanced" else "3fr"
        self._layout_mode = mode
        if mode == "balanced" and self.is_mounted:
            self._render_host_header()
        if persist:
            self._save_tui_preferences()

    def _handle_layout_cmd(self, text: str):
        chat = self.query_one("#chat-history", ChatHistory)
        parts = text.split(maxsplit=1)
        requested = parts[1].strip().casefold() if len(parts) > 1 else ""
        aliases = {"reset": "chat", "default": "chat"}
        mode = aliases.get(requested, requested)
        if not mode:
            chat.write(markup(f"[#c7ff8a]Layout · {self._layout_mode} · Use /layout chat|balanced|compact[/]"))
            return
        if mode not in {"chat", "balanced", "compact"}:
            chat.write(markup("[#ff7a7a]Unknown layout. Use /layout chat|balanced|compact|reset.[/]"))
            return
        self._apply_layout(mode)
        chat.write(markup(f"[#63ffd9]✓ Layout · {mode}[/]"))

    def _reset_side_panel_data(self) -> None:
        self._side_panel_overview = None
        self._side_panel_scheduler_jobs = None
        self._side_panel_background_jobs = None
        self._side_panel_data_agent = None
        self._side_panel_loading = False
        self._side_panel_incomplete = False

    def _cancel_side_panel_refresh(self) -> None:
        if self._side_panel_refresh_task and not self._side_panel_refresh_task.done():
            self._side_panel_refresh_task.cancel()
        self._side_panel_refresh_task = None

    def _render_side_panel(self) -> None:
        if not self.is_mounted:
            return
        self.query_one("#side-panel", SidePanel).update_dashboard(
            instance_id=self.current_instance_id,
            current_agent=self.current_agent,
            current_agent_display=self.current_agent_display,
            current_backend=self.current_backend,
            gateway_ok=self.gateway_ok,
            overview=self._side_panel_overview,
            scheduler_jobs=self._side_panel_scheduler_jobs,
            background_jobs=self._side_panel_background_jobs,
            agents=self._agents_cache,
            language=self._ui_language,
            loading=self._side_panel_loading,
            incomplete=self._side_panel_incomplete,
        )

    def _apply_side_panel_visibility(self, *, persist: bool = True) -> None:
        if not self.is_mounted:
            if persist:
                self._save_tui_preferences()
            return
        panel = self.query_one("#side-panel", SidePanel)
        panel.set_auto_scroll(self._side_panel_auto_scroll)
        panel.display = self._side_panel_enabled
        if self._side_panel_enabled:
            self._render_side_panel()
        else:
            self._cancel_side_panel_refresh()
        if persist:
            self._save_tui_preferences()

    def _schedule_side_panel_refresh(self) -> None:
        if (
            not self.is_mounted
            or not self._side_panel_enabled
            or not self.gateway_ok
            or not self.current_agent
        ):
            self._render_side_panel()
            return
        if self._side_panel_refresh_task and not self._side_panel_refresh_task.done():
            return
        self._side_panel_refresh_task = asyncio.create_task(
            self._refresh_side_panel(
                client=self.api,
                generation=self._connection_generation,
                agent=self.current_agent,
            )
        )

    async def _refresh_side_panel(
        self,
        *,
        client: TuiApiClient | None = None,
        generation: int | None = None,
        agent: str | None = None,
    ) -> None:
        if not self._side_panel_enabled:
            return
        client = client or self.api
        generation = self._connection_generation if generation is None else generation
        agent = agent or self.current_agent
        if not agent or not self.gateway_ok:
            self._render_side_panel()
            return
        if self._side_panel_data_agent != agent:
            self._reset_side_panel_data()
            self._side_panel_data_agent = agent
        self._side_panel_loading = True
        self._render_side_panel()
        results = await asyncio.gather(
            client.agent_overview(agent),
            client.scheduler_jobs(agent),
            client.background_jobs(agent, limit=20),
            return_exceptions=True,
        )
        if (
            not self._side_panel_enabled
            or generation != self._connection_generation
            or client is not self.api
            or agent != self.current_agent
        ):
            logger.debug("Discarded stale TUI side-panel refresh: agent=%s generation=%s", agent, generation)
            return

        overview_result, scheduler_result, background_result = results
        incomplete = False
        if isinstance(overview_result, dict) and overview_result.get("ok"):
            overview = overview_result.get("overview")
            self._side_panel_overview = overview if isinstance(overview, dict) else None
            incomplete = self._side_panel_overview is None
        else:
            self._side_panel_overview = None
            incomplete = True
        if isinstance(scheduler_result, dict) and scheduler_result.get("ok"):
            jobs = scheduler_result.get("jobs")
            self._side_panel_scheduler_jobs = jobs if isinstance(jobs, list) else []
        else:
            self._side_panel_scheduler_jobs = None
            incomplete = True
        if isinstance(background_result, dict) and background_result.get("ok"):
            jobs = background_result.get("jobs")
            self._side_panel_background_jobs = jobs if isinstance(jobs, list) else []
        else:
            self._side_panel_background_jobs = None
            incomplete = True
        self._side_panel_loading = False
        self._side_panel_incomplete = incomplete
        self._side_panel_data_agent = agent
        self._render_side_panel()

    async def _handle_sidepanel_cmd(self, text: str) -> None:
        chat = self.query_one("#chat-history", ChatHistory)
        parts = text.split()
        if len(parts) >= 2 and parts[1].casefold() == "auto":
            if len(parts) == 2:
                state = "ON" if self._side_panel_auto_scroll else "OFF"
                message = (
                    f"自动巡览 · {state}。用法 · /sidepanel auto on|off|toggle"
                    if self._ui_language == "zh"
                    else f"Automatic tour · {state}. Usage · /sidepanel auto on|off|toggle"
                )
                chat.write(Text(message, style="#c7ff8a"))
                return
            auto_action = parts[2].casefold() if len(parts) == 3 else ""
            if auto_action not in {"on", "off", "toggle"}:
                message = (
                    "请使用 /sidepanel auto on|off|toggle。"
                    if self._ui_language == "zh"
                    else "Use /sidepanel auto on|off|toggle."
                )
                chat.write(Text(message, style="#ff7a7a"))
                return
            self._side_panel_auto_scroll = (
                not self._side_panel_auto_scroll
                if auto_action == "toggle"
                else auto_action == "on"
            )
            if self._side_panel_auto_scroll:
                self._side_panel_enabled = True
            panel = self.query_one("#side-panel", SidePanel)
            panel.set_auto_scroll(
                self._side_panel_auto_scroll,
                reset=self._side_panel_auto_scroll,
            )
            self._cancel_side_panel_refresh()
            self._apply_side_panel_visibility()
            if self._side_panel_enabled and self.gateway_ok and self.current_agent:
                await self._refresh_side_panel(
                    client=self.api,
                    generation=self._connection_generation,
                    agent=self.current_agent,
                )
            state = "ON" if self._side_panel_auto_scroll else "OFF"
            message = (
                f"✓ 自动巡览已设为 {state}。"
                if self._ui_language == "zh"
                else f"✓ Automatic tour set to {state}."
            )
            chat.write(Text(message, style="#63ffd9"))
            return
        action = parts[1].casefold() if len(parts) == 2 else "on"
        if len(parts) > 2 or action not in {"on", "off", "toggle", "refresh"}:
            message = (
                "请使用 /sidepanel on|off|toggle|refresh 或 /sidepanel auto on|off|toggle。"
                if self._ui_language == "zh"
                else "Use /sidepanel on|off|toggle|refresh or /sidepanel auto on|off|toggle."
            )
            chat.write(Text(message, style="#ff7a7a"))
            return
        self._side_panel_enabled = (
            not self._side_panel_enabled if action == "toggle" else action != "off"
        )
        self._cancel_side_panel_refresh()
        self._apply_side_panel_visibility()
        if self._side_panel_enabled:
            await self._refresh_side_panel(
                client=self.api,
                generation=self._connection_generation,
                agent=self.current_agent,
            )
        if self._ui_language == "zh":
            message = f"✓ 只读信息面板{'已打开' if self._side_panel_enabled else '已关闭'}。"
        else:
            message = f"✓ Read-only information panel {'opened' if self._side_panel_enabled else 'closed'}."
        chat.write(Text(message, style="#63ffd9"))

    def _handle_log_cmd(self, text: str):
        chat = self.query_one("#chat-history", ChatHistory)
        parts = text.split(maxsplit=1)
        action = parts[1].strip().casefold() if len(parts) > 1 else "pause"
        if action == "show":
            self._apply_layout("chat")
            chat.write(markup("[#63ffd9]✓ Host log shown.[/]"))
        elif action == "hide":
            self._apply_layout("compact")
            chat.write(markup("[#63ffd9]✓ Host log hidden.[/]"))
        elif action == "pause":
            self.action_toggle_log_pause()
            state = "paused" if self._log_paused else "following"
            chat.write(markup(f"[#63ffd9]✓ Host log · {state}.[/]"))
        else:
            chat.write(markup("[#ff7a7a]Use /log show|hide|pause.[/]"))

    @work()
    async def _send_message(
        self,
        text: str,
        agent: str,
        client: TuiApiClient,
        generation: int,
        telegram_mirror: bool,
        ui_locale: str,
        submission_ref: tuple[int, str, int],
    ):
        result = await client.send_chat(
            agent,
            text,
            client_id=self._tui_client_id,
            telegram_mirror=telegram_mirror,
            ui_locale=ui_locale,
        )
        if generation != self._connection_generation or client is not self.api:
            logger.info("TUI message completed on previous instance generation=%s agent=%s", generation, agent)
            return

        if result.get("slash_command"):
            self._render_command_result(result, agent=agent, submitted_text=text)
            if str(result.get("command") or "").casefold() in {"stop", "cancel"}:
                self._clear_typing_indicator()
            await self._refresh_runtime_state(client=client, generation=generation)
            return

        if not result.get("ok", True):
            self._clear_typing_indicator()
            error = str(result.get("error") or "Request was rejected")
            self.query_one("#chat-history", ChatHistory).write(
                Text(f"Error ({agent}): {error}", style="red")
            )
            return

        session_id = str(result.get("session_id") or "").strip()
        run_id = str(result.get("run_id") or "").strip()
        request_id = str(result.get("request_id") or "").strip()
        if not session_id or not run_id:
            logger.warning(
                "TUI submission returned no trackable Run: agent=%s request=%s",
                agent,
                request_id or "missing",
            )
            if self._latest_submission_ref == submission_ref:
                self._clear_typing_indicator()
            return
        if self._latest_submission_ref != submission_ref:
            logger.debug("Ignored stale TUI Run indicator: submission=%s", submission_ref)
            return

        run_ref = (generation, agent, session_id, run_id, request_id)
        self._set_typing_run(run_ref, phase="queued")
        status_failures = 0

        while self._active_run_ref == run_ref:
            status = await client.run_info(session_id, run_id)
            if (
                generation != self._connection_generation
                or client is not self.api
                or self._active_run_ref != run_ref
            ):
                return
            if not status.get("ok"):
                status_failures += 1
                logger.warning(
                    "TUI could not track submitted Run: agent=%s session=%s run=%s error=%s",
                    agent,
                    session_id,
                    run_id,
                    status.get("error"),
                )
                if status_failures < 3:
                    await asyncio.sleep(0.5)
                    continue
                self._clear_typing_indicator(run_ref)
                self.query_one("#chat-history", ChatHistory).write(
                    Text(
                        f"Run status unavailable ({agent}): "
                        f"{status.get('error') or 'unknown error'}",
                        style="red",
                    )
                )
                return
            status_failures = 0
            run = status.get("run")
            state = (
                str(run.get("state") or "").strip().casefold()
                if isinstance(run, dict)
                else ""
            )
            if state in TUI_TERMINAL_RUN_STATES:
                failure = run_failure_text(status)
                if failure:
                    chat = self.query_one("#chat-history", ChatHistory)
                    chat.write(Text(f"Request failed ({agent}): {failure}", style="red"))
                self._clear_typing_indicator(run_ref)
                return
            self._set_typing_run(
                run_ref,
                phase="running" if state == "running" else "queued",
            )
            await asyncio.sleep(0.5)

    def _render_command_result(
        self,
        result: dict,
        *,
        agent: str,
        submitted_text: str = "",
    ) -> None:
        chat = self.query_one("#chat-history", ChatHistory)
        messages = result.get("messages")
        rendered = False
        if isinstance(messages, list):
            for message in messages:
                if not isinstance(message, dict) or not str(message.get("text") or ""):
                    continue
                chat.write(command_message_renderable(message))
                rendered = True
        if not result.get("ok", True):
            error = str(result.get("error") or "Command failed")
            if not rendered or not any(
                error in str(message.get("text") or "")
                for message in messages or []
                if isinstance(message, dict)
            ):
                chat.write(Text(f"Command failed ({agent}): {error}", style="red"))
        command = str(result.get("command") or "").strip().casefold()
        submitted_parts = submitted_text.strip().split()
        if (
            result.get("ok", True)
            and command
            and len(submitted_parts) == 1
            and submitted_parts[0].casefold() == f"/{command}"
        ):
            details = self._command_guide_details(command, include_usage=True)
            if details:
                chat.write(Text("\n".join(details), style="dim #9be7ff"))

    async def _refresh_runtime_state(
        self,
        *,
        client: TuiApiClient,
        generation: int,
    ) -> None:
        agents = await client.list_agents()
        if generation != self._connection_generation or client is not self.api:
            return
        self._adopt_agent_directory(agents)
        self._update_status_bar()
        self._schedule_side_panel_refresh()

    def _set_typing_run(
        self,
        run_ref: tuple[int, str, str, str, str],
        *,
        phase: str,
    ) -> None:
        if run_ref[0] != self._connection_generation or run_ref[1] != self.current_agent:
            return
        self._active_run_ref = run_ref
        self._active_run_phase = "running" if phase == "running" else "queued"
        self._refresh_typing_indicator()

    def _refresh_typing_indicator(self) -> None:
        indicator = self.query_one("#typing-indicator", TypingIndicator)
        run_ref = self._active_run_ref
        if (
            not self._tui_typing_enabled
            or run_ref is None
            or run_ref[0] != self._connection_generation
            or run_ref[1] != self.current_agent
        ):
            indicator.clear_run()
            return
        indicator.show_run(
            agent=self.current_agent_display or run_ref[1],
            phase=self._active_run_phase,
            language=self._ui_language,
        )

    def _clear_typing_indicator(
        self,
        run_ref: tuple[int, str, str, str, str] | None = None,
    ) -> None:
        if run_ref is not None and self._active_run_ref != run_ref:
            return
        self._active_run_ref = None
        self._active_run_phase = "idle"
        if self.is_mounted:
            self.query_one("#typing-indicator", TypingIndicator).clear_run()

    def _clear_typing_for_transcript_message(self, message: dict) -> None:
        run_ref = self._active_run_ref
        if run_ref is None:
            return
        message_run = str(message.get("run_id") or "").strip()
        message_request = str(message.get("request_id") or "").strip()
        if (message_run and message_run == run_ref[3]) or (
            message_request and message_request == run_ref[4]
        ):
            self._clear_typing_indicator(run_ref)

    @work()
    async def _send_broadcast(
        self,
        text: str,
        client: TuiApiClient,
        generation: int,
        telegram_mirror: bool,
        ui_locale: str,
    ):
        agents = await client.list_agents()
        for a in agents:
            if a.get("is_active") or a.get("online"):
                await client.send_chat(
                    a["name"],
                    text,
                    client_id=self._tui_client_id,
                    telegram_mirror=telegram_mirror,
                    ui_locale=ui_locale,
                )
        if generation != self._connection_generation:
            logger.info("TUI broadcast completed on previous instance generation=%s", generation)

    # ── /to command ─────────────────────────────────────────────────────

    async def _handle_to(self, text: str):
        chat = self.query_one("#chat-history", ChatHistory)
        parts = text.split()[1:]  # strip "/to"
        if not parts:
            chat.write(markup("[#c7ff8a]Usage: /to <agent> or /to all[/]"))
            return

        # Refresh agent list
        agents = await self.api.list_agents()
        self._adopt_agent_directory(agents)
        agent_map = {a["name"]: a for a in agents}

        target = parts[0].lower()
        if target == "all":
            self._clear_typing_indicator()
            self._latest_submission_ref = None
            # Multi-cast mode — just switch display; actual sending done in send
            active_targets = [
                a["name"]
                for a in agents
                if a.get("is_active") or a.get("online")
            ]
            self.current_agent = None
            self._chat_targets = active_targets
            self.current_agent_display = "ALL"
            self.current_backend = ""
            self._current_agent_metadata = {}
            self._cancel_side_panel_refresh()
            self._reset_side_panel_data()
            chat.border_title = "Chat \u2014 \U0001f4e2 Broadcasting to ALL agents"
            chat.write(markup("[#63ffd9]\u2705 Broadcasting mode: messages will be sent to all active agents.[/]"))
            self._update_status_bar()
            self._render_side_panel()
            return

        # Single or multi agent
        if target in agent_map:
            self._select_agent(agent_map[target])
            chat.write(markup(f"[#63ffd9]\u2705 Switched to {self.current_agent_display}[/]"))
        else:
            chat.write(markup(f"[#ff7a7a]Agent '{target}' not found. Use /agents to list.[/]"))

    async def _handle_agents_cmd(self):
        chat = self.query_one("#chat-history", ChatHistory)
        agents = await self.api.list_agents()
        self._adopt_agent_directory(agents)
        if not agents:
            chat.write(markup("[#c7ff8a]No agents found.[/]"))
            return
        chat.write(markup("[bold #9be7ff]Available agents:[/]"))
        for a in agents:
            emoji = a.get("emoji", "")
            name = a.get("name", "?")
            display = a.get("display_name", name)
            engine = a.get("active_backend", a.get("engine", "?"))
            online = "[#63ffd9]\U0001f7e2[/]" if a.get("online") else "[#7fb6c7]\u26aa[/]"
            marker = " \u25c0" if name == self.current_agent else ""
            chat.write(markup(f"  {online} {emoji} [#dff6ff]{name}[/] ([#9be7ff]{display}[/]) [#71b7ff]\u2014[/] [#7fb6c7]{engine}[/]{marker}"))

    # ── /instance command ──────────────────────────────────────────────

    def _client_for_instance(self, target: InstanceTarget) -> TuiApiClient:
        if target.transport == "direct":
            urls = list(target.workbench_urls)
            return TuiApiClient(
                base_url=urls[0],
                fallback_base_urls=urls[1:],
                expected_instance_id=target.instance_id,
            )
        return TuiApiClient(
            remote_url=target.remote_url,
            target_instance=target.instance_id,
            expected_instance_id=target.instance_id,
        )

    async def _show_instances(self, *, refresh: bool = True) -> list[InstanceTarget]:
        chat = self.query_one("#chat-history", ChatHistory)
        targets = await self._instance_resolver.discover(refresh=refresh)
        chat.write(markup("[bold #9be7ff]HASHI instances:[/]"))
        for target in targets:
            selected = target.instance_id == self.current_instance_id
            if selected:
                icon = "[#63ffd9]●[/]"
                state = "current · connected"
            elif target.available:
                icon = "[#9be7ff]○[/]"
                state = f"available · {target.route_kind}"
            else:
                icon = "[#ff7a7a]×[/]"
                state = target.reason or "unavailable"
            chat.write(markup(f"  {icon} [#dff6ff]{target.instance_id}[/]  [#7fb6c7]{state}[/]"))
        if len(targets) == 1:
            chat.write(
                markup(
                    "[#c7ff8a]No trusted peers available. Hashi Remote must be "
                    "on and handshaken on both instances.[/]"
                )
            )
        chat.write(markup("[#c7ff8a]Use: /instance <id> · /instance current · /instance refresh[/]"))
        return targets

    async def _handle_instance_cmd(self, text: str):
        chat = self.query_one("#chat-history", ChatHistory)
        parts = text.split(maxsplit=1)
        argument = parts[1].strip() if len(parts) > 1 else ""
        if not argument or argument.lower() == "refresh":
            await self._show_instances(refresh=True)
            return

        requested = self.launch_instance_id if argument.lower() == "current" else argument.upper()
        if requested == self.current_instance_id:
            chat.write(markup(f"[#c7ff8a]Already connected to {self.current_instance_id}.[/]"))
            return

        async with self._instance_switch_lock:
            targets = await self._instance_resolver.discover(refresh=True)
            target = next((item for item in targets if item.instance_id == requested), None)
            if target is None:
                chat.write(markup(f"[#ff7a7a]Instance '{requested}' was not found through local Hashi Remote.[/]"))
                return
            if not target.available:
                chat.write(markup(f"[#ff7a7a]Cannot switch to {requested}: {target.reason or 'unavailable'}.[/]"))
                return

            candidate = self._client_for_instance(target)
            self._write_log_line(
                f"[TUI] Verifying instance switch {self.current_instance_id} -> {target.instance_id}..."
            )
            health = await candidate.health_info()
            if not health.get("ok"):
                reason = str(health.get("error") or "health check failed")
                logger.warning(
                    "TUI instance switch rolled back: from=%s target=%s reason=%s",
                    self.current_instance_id,
                    target.instance_id,
                    reason,
                )
                self._write_log_line(f"[TUI] Instance switch rejected; keeping {self.current_instance_id}: {reason}")
                chat.write(markup(f"[#ff7a7a]Switch failed: {reason}. Current connection was kept.[/]"))
                return
            agent_result = await candidate.agents_info()
            if not agent_result.get("ok") or not isinstance(agent_result.get("agents"), list):
                reason = str(agent_result.get("error") or "agent directory unavailable")
                logger.warning(
                    "TUI instance switch rolled back: from=%s target=%s reason=%s",
                    self.current_instance_id,
                    target.instance_id,
                    reason,
                )
                self._write_log_line(f"[TUI] Instance switch rejected; keeping {self.current_instance_id}: {reason}")
                chat.write(markup(f"[#ff7a7a]Switch failed: {reason}. Current connection was kept.[/]"))
                return
            agents = agent_result["agents"]

            previous_instance = self.current_instance_id
            self._clear_typing_indicator()
            self._latest_submission_ref = None
            self._cancel_side_panel_refresh()
            self._reset_side_panel_data()
            self._connection_generation += 1
            generation = self._connection_generation
            self.api = candidate
            self.current_instance_id = target.instance_id
            self.gateway_ok = True
            self.current_agent = None
            self._chat_targets = []
            self.current_agent_display = ""
            self.current_backend = ""
            self._current_agent_metadata = {}
            self._agents_cache = []
            self._agent_refresh_tick = 0
            chat.clear()
            location = self._location_label()
            chat_label = "聊天" if self._ui_language == "zh" else "Chat"
            chat.border_title = f"{chat_label} · {self.current_instance_id} ({location})"
            await self._load_agents(
                client=candidate,
                generation=generation,
                agents=agents,
            )
            self._update_status_bar()
            logger.info(
                "TUI instance switch committed: from=%s target=%s transport=%s",
                previous_instance,
                self.current_instance_id,
                target.transport,
            )
            self._write_log_line(
                f"[TUI] Connected to {self.current_instance_id} via "
                f"{'local Workbench' if target.transport == 'direct' else 'authenticated Hashi Remote'}."
            )
            chat.write(markup(f"[#63ffd9]✅ Connected to {self.current_instance_id}.[/]"))

    # ── Status bar ──────────────────────────────────────────────────────

    def _update_status_bar(self):
        bar = self.query_one("#footer-info-box", FooterInfoBox)
        agent = self.current_agent_display or (self.current_agent or "")
        bar.update_state(
            agent,
            self.current_backend,
            self.gateway_ok,
            self._agents_cache,
            self.current_agent,
            self.current_instance_id,
            self._ui_language,
            self._current_agent_metadata,
            self._telegram_mirror_enabled,
        )

    # ── Actions ─────────────────────────────────────────────────────────

    def action_toggle_log_pause(self):
        self._log_paused = not self._log_paused
        log = self.query_one("#log-panel", LogPanel)
        base_title = (
            f"主机日志 · {self.launch_instance_id}（本机）"
            if self._ui_language == "zh"
            else f"Host log · {self.launch_instance_id} (local)"
        )
        paused = " [已暂停]" if self._ui_language == "zh" else " [PAUSED]"
        log.border_title = (
            base_title + (paused if self._log_paused else "")
        )

    async def action_quit_app(self):
        await self._shutdown()

    async def _shutdown(self):
        self._clear_typing_indicator()
        self._cancel_side_panel_refresh()
        if self._log_follow_task and not self._log_follow_task.done():
            self._log_follow_task.cancel()
        if self.bridge_proc and self.bridge_proc.returncode is None:
            self._write_log_line("[TUI] Shutting down HASHI...")
            self.bridge_proc.terminate()
            try:
                await asyncio.wait_for(self.bridge_proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.bridge_proc.kill()
        self.exit()
